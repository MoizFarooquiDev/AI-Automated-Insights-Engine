"""
Solar insights generator.

Reads processed + raw + forecast dataframes for a site and emits a list of
insight records covering Under Performance, Fault, SLA, and Forecast families.
"""

import re
import statistics

import pandas as pd
import pvlib
from pvlib.temperature import TEMPERATURE_MODEL_PARAMETERS

from apis import get_site_db, get_std_details, get_description
from thresholds import SOLAR as T


def _last(df, col):
    """Return df[col].iloc[-1] if the column exists and is not all-NaN, else 0."""
    if col in df.columns and not df[col].isna().all():
        return df[col].iloc[-1]
    return 0


def _human_list(parts):
    """Join a list of strings as a natural English enumeration."""
    if len(parts) == 1:
        return parts[0]
    if len(parts) == 2:
        return f"{parts[0]} and {parts[1]}"
    return ", ".join(parts[:-1]) + f", and {parts[-1]}"


def solar(df_processed, df_raw, df_forecast, login_response, mapping, site_id):

    solar_results = []

    # ── Site setup ────────────────────────────────────────────────────────
    try:
        site_json    = get_site_db(site_id, login_response)
        name         = site_json.get("siteInfo", {}).get("siteName", "")
        system_size  = site_json.get("siteInfo", {}).get("siteSolarPower", 0)
        temp_model   = site_json.get("siteInfo", {}).get("TemperatureModel", "open_rack_glass_glass")
        gamma_pdc    = site_json.get("siteInfo", {}).get("gamma_pdc", -0.0035)
        grid_tariff  = site_json.get("siteInfo", {}).get("siteGridTarrif", 25.29)
        installation = site_json.get("InstallationModelization", {}).get("Inverter", {})

        df  = df_processed.copy()
        df2 = df_raw.copy()

        _, _, rawMapping   = get_std_details(site_json)
        reverseMapping     = {v: k for k, v in rawMapping.items() if v}

        req_fields  = ["TIMESTAMP", "PSOLAR", "POAI"]
        df2         = df2.reset_index()
        df2.columns = [rawMapping.get(c, c) for c in df2.columns]
        missing_cols = list(set(req_fields) - set(df2.columns))
        df2 = pd.concat([df2, pd.DataFrame(columns=missing_cols)], axis=1)[req_fields]
        df2["TIMESTAMP"] = pd.to_datetime(df2["TIMESTAMP"], errors="coerce")
        df2.set_index("TIMESTAMP", inplace=True)
        df2 = df2[df2.index.notna()].resample("min").mean().reset_index()
        df2.dropna(subset=["POAI"], how="all", inplace=True)

    except Exception as e:
        print(f"[{site_id}] Site setup error: {e}")
        return []

    def append(cat, sub, det, desc, sev, opp_cost=0):
        solar_results.append({
            "siteName"        : name,
            "siteId"          : site_id,
            "category"        : cat,
            "subCategory"     : sub,
            "detailCategory"  : det,
            "description"     : desc,
            "status"          : 0,
            "sev"             : sev,
            "opportunityCost" : round(float(opp_cost), 2),
        })

    # ── 0,1,0  Low Daily Generation ───────────────────────────────────────
    try:
        kwh = _last(df, "kWh")
        target_value = (
            _last(df, "targetValue")       if "targetValue"       in df.columns and not df["targetValue"].isna().all()       else
            _last(df, "PVsyst.client.P90") if "PVsyst.client.P90" in df.columns and not df["PVsyst.client.P90"].isna().all() else
            _last(df, "forecasted_kwh")    if "forecasted_kwh"    in df.columns and not df["forecasted_kwh"].isna().all()    else
            0
        )
        if target_value > kwh:
            total_loss = target_value - kwh
            detail     = get_description(mapping, 0, 1, 0)
            append(0, 1, 0,
                   detail["desc"].format(value=round(float(kwh), 2), value1=round(float(target_value), 2)),
                   detail["Sev"],
                   opp_cost=total_loss * grid_tariff)
    except Exception as e:
        print(f"[{site_id}] Low Daily Generation (0,1,0) error: {e}")

    # ── 0,1,1  Low Daily PR ───────────────────────────────────────────────
    try:
        pr          = _last(df, "pr")
        target_pr   = _last(df, "PVsyst.client.PR")
        irradiation = _last(df, "irradiation")
        if target_pr > pr:
            total_loss = ((target_pr - pr) / 100) * irradiation * system_size
            detail     = get_description(mapping, 0, 1, 1)
            append(0, 1, 1,
                   detail["desc"].format(value=round(float(pr), 2), value1=round(float(target_pr), 2)),
                   detail["Sev"],
                   opp_cost=total_loss * grid_tariff)
    except Exception as e:
        print(f"[{site_id}] Low Daily PR (0,1,1) error: {e}")

    # ── 0,1,2  Underperforming Inverters ──────────────────────────────────
    try:
        inverter_yields = {
            inv_id: _last(df, f"inverters.{inv_id}.yield")
            for inv_id in installation
            if f"inverters.{inv_id}.yield" in df.columns and not df[f"inverters.{inv_id}.yield"].isna().all()
        }
        if inverter_yields:
            detail   = get_description(mapping, 0, 1, 2)
            areas    = {}
            has_area = False
            for inv_id, inv_data in installation.items():
                area = inv_data.get("Area")
                if area:
                    has_area = True
                    areas.setdefault(area, []).append(inv_id)

            all_underperforming = []
            if has_area:
                for area, inv_ids in areas.items():
                    try:
                        area_yields = {i: inverter_yields[i] for i in inv_ids if i in inverter_yields}
                        if not area_yields:
                            continue
                        max_yield = max(area_yields.values())
                        if max_yield == 0:
                            continue
                        for inv_id in sorted(
                            [i for i, y in area_yields.items() if (max_yield - y) / max_yield * 100 > T["underperforming_inverters"]["max_deviation_pct"]],
                            key=lambda x: int(x.replace("INV", "")),
                        ):
                            all_underperforming.append((inv_id, max_yield))
                    except Exception as e:
                        print(f"[{site_id}] Inverter area '{area}' error: {e}")
            else:
                max_yield = max(inverter_yields.values())
                if max_yield > 0:
                    for inv_id in sorted(
                        [i for i, y in inverter_yields.items() if (max_yield - y) / max_yield * 100 > T["underperforming_inverters"]["max_deviation_pct"]],
                        key=lambda x: int(x.replace("INV", "")),
                    ):
                        all_underperforming.append((inv_id, max_yield))

            if all_underperforming:
                inv_labels = ", ".join(i for i, _ in all_underperforming)
                max_yields = " and ".join(
                    str(round(float(my), 2))
                    for my in dict.fromkeys(my for _, my in all_underperforming)
                )
                total_loss = sum(
                    (max_yield - inverter_yields[inv_id]) * installation[inv_id].get("DcPower", 0)
                    for inv_id, max_yield in all_underperforming
                )
                append(0, 1, 2,
                       detail["desc"].format(value=inv_labels, value1=max_yields),
                       detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] Underperforming Inverters (0,1,2) error: {e}")

    # ── 0,1,3  Underperforming Strings ────────────────────────────────────
    try:
        inv_string_results = {}
        for inv_id, inv_data in installation.items():
            try:
                strings = inv_data.get("Strings", {})
                if not strings:
                    continue
                string_yields = {}
                for string_id in strings:
                    col = f"inverters.{inv_id}.string.yield.{string_id}"
                    if col not in df.columns or df[col].isna().all():
                        continue
                    raw_val = df[col].iloc[-1]
                    if isinstance(raw_val, (int, float)):
                        parsed = float(raw_val)
                    else:
                        m      = re.search(r"\(([^)]+)\)", str(raw_val))
                        parsed = float(m.group(1)) if m else None
                    if parsed and parsed > 0:
                        string_yields[string_id] = parsed
                if not string_yields:
                    continue
                ref = statistics.mean(sorted(string_yields.values(), reverse=True)[:3])
                if ref == 0:
                    continue
                underperforming = sorted(
                    [s for s, y in string_yields.items() if (ref - y) / ref * 100 > T["underperforming_strings"]["max_deviation_pct"]],
                    key=lambda x: int(x),
                )
                if underperforming:
                    inv_string_results[inv_id] = underperforming
            except Exception as e:
                print(f"[{site_id}] Strings for {inv_id} error: {e}")

        if inv_string_results:
            parts = [
                f"Strings {', '.join(str(s) for s in s_ids)} of {inv_id}"
                for inv_id, s_ids in sorted(
                    inv_string_results.items(),
                    key=lambda x: int(x[0].replace("INV", "")),
                )
            ]
            append(0, 1, 3,
                   f"{_human_list(parts)} have a deviation exceeding {T['underperforming_strings']['max_deviation_pct']}% "
                   f"compared to the best-performing string of the respective inverter.",
                   "Medium")
    except Exception as e:
        print(f"[{site_id}] Underperforming Strings (0,1,3) error: {e}")

    # ── 0,1,4  Soiling Trend ──────────────────────────────────────────────
    try:
        if "pr" in df.columns and not df["pr"].isna().all():
            pr_values = df["pr"].dropna().tolist()
            if len(pr_values) >= 2 and all(pr_values[i] > pr_values[i + 1] for i in range(len(pr_values) - 1)):
                avg_drop = round((pr_values[0] - pr_values[-1]) / (len(pr_values) - 1), 4)
                detail   = get_description(mapping, 0, 1, 4)
                append(0, 1, 4, detail["desc"].format(value=avg_drop), detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] Soiling Trend (0,1,4) error: {e}")

    # ── 0,1,5  Thermal Derating ───────────────────────────────────────────
    try:
        kwh          = _last(df, "kWh")
        avg_mod_temp = _last(df, "avg_mod_temp")
        if kwh > 0 and avg_mod_temp > 0:
            avg_poa = df2.loc[df2["POAI"] >= 0, "POAI"].mean()
            avg_poa = round(float(avg_poa), 2) if not pd.isna(avg_poa) else 0
            temp_params = TEMPERATURE_MODEL_PARAMETERS["sapm"][temp_model]
            try:
                ideal_temp = float(pvlib.temperature.sapm_cell(
                    poa_global=avg_poa,
                    temp_air=T["thermal_derating"]["temp_air_c"],
                    wind_speed=T["thermal_derating"]["wind_speed_ms"],
                    a=temp_params["a"], b=temp_params["b"], deltaT=temp_params["deltaT"],
                ))
                if not ideal_temp or pd.isna(ideal_temp) or ideal_temp < T["thermal_derating"]["ideal_temp_floor_c"]:
                    ideal_temp = T["thermal_derating"]["ideal_temp_floor_c"]
            except Exception:
                ideal_temp = T["thermal_derating"]["ideal_temp_floor_c"]

            irradiation     = _last(df, "irradiation")
            actual_pr       = kwh / (irradiation * system_size) * 100
            temp_correction = 1 + gamma_pdc * (avg_mod_temp - ideal_temp)
            temp_loss       = round((actual_pr / temp_correction / 100) * irradiation * system_size - kwh, 2)

            if temp_loss > 0:
                detail = get_description(mapping, 0, 1, 5)
                append(0, 1, 5,
                       detail["desc"].format(value=round(float(avg_mod_temp), 2), value1=temp_loss),
                       detail["Sev"],
                       opp_cost=temp_loss * grid_tariff)
    except Exception as e:
        print(f"[{site_id}] Thermal Derating (0,1,5) error: {e}")

    # ── 0,1,7  Inverter Thermal Derating ─────────────────────────────────
    try:
        target_inv_temp = T.get("inverter_thermal", {}).get("target_temp_c", 40)
        k               = T.get("inverter_thermal", {}).get("derating_coeff", 0.005)

        inv_tint_map = {}
        for inv_id in installation:
            raw_col = reverseMapping.get(f"{inv_id}_TINT")
            if raw_col and raw_col in df_raw.columns:
                inv_tint_map[inv_id] = raw_col

        if inv_tint_map:
            poai_raw       = reverseMapping.get("POAI")
            tint_raw_cols  = list(inv_tint_map.values())
            req_raw_fields = ["TIMESTAMP", poai_raw] + tint_raw_cols

            df4 = df_raw.copy().reset_index()
            df4.rename(columns={"@timestamp": "TIMESTAMP"}, inplace=True)
            missing = list(set(req_raw_fields) - set(df4.columns))
            df4 = pd.concat([df4, pd.DataFrame(columns=missing)], axis=1)[req_raw_fields]

            rename_map = {poai_raw: "POAI"}
            rename_map.update({raw: f"{inv_id}_TINT" for inv_id, raw in inv_tint_map.items()})
            df4.rename(columns=rename_map, inplace=True)

            tint_std_cols = [f"{inv_id}_TINT" for inv_id in inv_tint_map]
            df4["TIMESTAMP"] = pd.to_datetime(df4["TIMESTAMP"], utc=True)
            df4.set_index("TIMESTAMP", inplace=True)
            df4.index = df4.index.tz_convert("Asia/Karachi")
            df4 = df4[df4.index.notna()].resample("min").mean()
            df4[tint_std_cols] = df4[tint_std_cols].ffill().bfill()
            df4["POAI"]        = df4["POAI"].ffill().bfill()

            df4_active   = df4[df4["POAI"] > 5]
            active_hours = len(df4_active) / 60
            breached     = []

            for inv_id in installation:
                try:
                    col = f"{inv_id}_TINT"
                    if col not in df4_active.columns or df4_active[col].isna().all():
                        continue
                    avg_temp = df4_active[col].mean()
                    if pd.notna(avg_temp) and avg_temp > target_inv_temp:
                        power_loss_kw   = round(installation[inv_id].get("InverterPower", 0) * k * (avg_temp - target_inv_temp), 2)
                        energy_loss_kwh = round(power_loss_kw * active_hours, 2)
                        breached.append((inv_id, round(float(avg_temp), 2), energy_loss_kwh))
                except Exception as e:
                    print(f"[{site_id}] TINT for {inv_id} error: {e}")

            if breached:
                total_loss = round(sum(b[2] for b in breached), 2)
                inv_desc   = ", ".join(
                    f"{inv_id} with temp {temp}°C (loss: {loss} kWh)"
                    for inv_id, temp, loss in sorted(breached, key=lambda x: int(x[0].replace("INV", "")))
                )
                detail = get_description(mapping, 0, 1, 7)
                append(0, 1, 7,
                       detail["desc"].format(desc=inv_desc, value1=target_inv_temp, value2=total_loss),
                       detail["Sev"],
                       opp_cost=total_loss * grid_tariff)
    except Exception as e:
        print(f"[{site_id}] Inverter Thermal Derating (0,1,7) error: {e}")

    # ── 1,1,0  Inverter Downtime ──────────────────────────────────────────
    try:
        inv_psolar_map = {}
        for inv_id in installation:
            raw_col = (
                reverseMapping.get(f"{inv_id}_PSOLAR")
                or reverseMapping.get(f"{inv_id}_PSOLAR_T")
                or reverseMapping.get(f"{inv_id}_PSOLAR_R")
            )
            if raw_col and raw_col in df_raw.columns:
                inv_psolar_map[inv_id] = raw_col

        if not inv_psolar_map:
            raise ValueError("No PSOLAR columns resolved from mapping")

        poai_raw       = reverseMapping.get("POAI")
        req_raw_fields = ["TIMESTAMP", poai_raw] + list(inv_psolar_map.values())

        df3 = df_raw.copy().reset_index()
        df3.rename(columns={"@timestamp": "TIMESTAMP"}, inplace=True)
        missing = list(set(req_raw_fields) - set(df3.columns))
        df3 = pd.concat([df3, pd.DataFrame(columns=missing)], axis=1)[req_raw_fields]

        rename_map = {poai_raw: "POAI"}
        rename_map.update({raw: f"{inv_id}_PSOLAR" for inv_id, raw in inv_psolar_map.items()})
        df3.rename(columns=rename_map, inplace=True)

        inv_psolar_map  = {inv_id: f"{inv_id}_PSOLAR" for inv_id in inv_psolar_map}
        psolar_std_cols = list(inv_psolar_map.values())

        df3["TIMESTAMP"] = pd.to_datetime(df3["TIMESTAMP"], utc=True)
        df3.set_index("TIMESTAMP", inplace=True)
        df3.index = df3.index.tz_convert("Asia/Karachi")
        df3 = df3[df3.index.notna()].resample("min").mean()
        df3[psolar_std_cols] = df3[psolar_std_cols].ffill().bfill()
        df3["POAI"]          = df3["POAI"].ffill().bfill()

        # Re-read raw POAI without ffill to get true daylight mask
        df3_raw_poai = df_raw.copy().reset_index()
        df3_raw_poai.rename(columns={"@timestamp": "TIMESTAMP"}, inplace=True)
        df3_raw_poai = df3_raw_poai[["TIMESTAMP", poai_raw]].copy()
        df3_raw_poai.rename(columns={poai_raw: "POAI_RAW"}, inplace=True)
        df3_raw_poai["TIMESTAMP"] = pd.to_datetime(df3_raw_poai["TIMESTAMP"], utc=True)
        df3_raw_poai.set_index("TIMESTAMP", inplace=True)
        df3_raw_poai.index = df3_raw_poai.index.tz_convert("Asia/Karachi")
        df3_raw_poai = df3_raw_poai.resample("min").mean()  # no ffill — NaN at night

        # Filter df3 to only actual daylight rows
        df3["POAI_RAW"] = df3_raw_poai["POAI_RAW"]
        df3 = df3[df3["POAI_RAW"] > T["inverter_downtime"]["poai_min_wm2"]].copy()
        df3.drop(columns=["POAI_RAW"], inplace=True)

        all_inv_events = []
        total_loss     = 0.0

        for inv_id in installation:
            try:
                psolar_col = inv_psolar_map.get(inv_id)
                if not psolar_col or psolar_col not in df3.columns:
                    continue
                dc_power = installation[inv_id].get("DcPower", 0)
                if dc_power == 0:
                    continue
                df3["ideal_power_kw"] = (df3["POAI"] / 1000) * dc_power
                df3["is_downtime"]    = (df3["POAI"] > T["inverter_downtime"]["poai_min_wm2"]) & df3[psolar_col].notna() & (df3[psolar_col] == 0)
                df3["downtime_group"] = (df3["is_downtime"] != df3["is_downtime"].shift()).cumsum()
                blocks                = df3[df3["is_downtime"]].groupby("downtime_group")
                if blocks.ngroups == 0:
                    continue
                events   = []
                inv_loss = 0.0
                for _, block in blocks:
                    loss_kwh  = round(block["ideal_power_kw"].sum() / 60, 4)
                    inv_loss += loss_kwh
                    events.append(
                        f"{block.index[0].strftime('%I:%M %p').lstrip('0')} "
                        f"to {block.index[-1].strftime('%I:%M %p').lstrip('0')}"
                    )
                if events:
                    total_loss += inv_loss
                    all_inv_events.append((inv_id, ", ".join(events), round(inv_loss, 2)))
            except Exception as e:
                print(f"[{site_id}] Downtime for {inv_id} error: {e}")

        if all_inv_events:
            inv_events_desc = _human_list([f"on {i} at {ev}" for i, ev, _ in all_inv_events])
            detail          = get_description(mapping, 1, 1, 0)
            append(1, 1, 0,
                   detail["desc"].format(
                       value=inv_events_desc,
                       value1=round(total_loss, 2),
                   ),
                   detail["Sev"],
                   opp_cost=total_loss * grid_tariff)

    except Exception as e:
        print(f"[{site_id}] Inverter Downtime (1,1,0) error: {e}")

    # ── 2,1,0  PR Below Commitment ────────────────────────────────────────
    try:
        pr          = _last(df, "pr")
        target_pr   = _last(df, "PVsyst.client.PR")
        irradiation = _last(df, "irradiation")
        if target_pr > pr:
            total_loss = ((target_pr - pr) / 100) * irradiation * system_size
            detail     = get_description(mapping, 2, 1, 0)
            append(2, 1, 0,
                   detail["desc"].format(value=round(float(pr), 2), value1=round(float(target_pr), 2)),
                   detail["Sev"],
                   opp_cost=total_loss * grid_tariff)
    except Exception as e:
        print(f"[{site_id}] PR Below Commitment (2,1,0) error: {e}")

    # ── 2,1,1  Energy Below Commitment ───────────────────────────────────
    try:
        kwh = _last(df, "kWh")
        target_value = (
            _last(df, "targetValue")       if "targetValue"       in df.columns and not df["targetValue"].isna().all()       else
            _last(df, "PVsyst.client.P90") if "PVsyst.client.P90" in df.columns and not df["PVsyst.client.P90"].isna().all() else
            _last(df, "forecasted_kwh")    if "forecasted_kwh"    in df.columns and not df["forecasted_kwh"].isna().all()    else
            0
        )
        if target_value > kwh:
            total_loss = target_value - kwh
            detail     = get_description(mapping, 2, 1, 1)
            append(2, 1, 1,
                   detail["desc"].format(value=round(float(kwh), 2), value1=round(float(target_value), 2)),
                   detail["Sev"],
                   opp_cost=total_loss * grid_tariff)
    except Exception as e:
        print(f"[{site_id}] Energy Below Commitment (2,1,1) error: {e}")

    # ── 2,1,2  Uptime Below Commitment ───────────────────────────────────
    try:
        uptime       = _last(df, "uptime_percent")
        target_value = T["uptime_below_commitment"]["target_uptime_pct"]
        if target_value > uptime:
            detail = get_description(mapping, 2, 1, 2)
            append(2, 1, 2,
                   detail["desc"].format(value=round(float(uptime), 2), value1=target_value),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] Uptime Below Commitment (2,1,2) error: {e}")

    # ── 3,1,0  Tomorrow's Forecast ────────────────────────────────────────
    try:
        forecasted_kwh         = _last(df_forecast, "forecasted_kwh")
        forecasted_irradiation = _last(df_forecast, "forecasted_irradiation")
        if forecasted_kwh > 0:
            detail = get_description(mapping, 3, 1, 0)
            append(3, 1, 0,
                   detail["desc"].format(value=round(float(forecasted_kwh), 2),
                                         value1=round(float(forecasted_irradiation), 2)),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] Tomorrow's Forecast (3,1,0) error: {e}")

    # ── 3,1,1  Today's Forecast vs Actual ────────────────────────────────
    try:
        forecasted_kwh         = _last(df, "forecasted_kwh")
        forecasted_irradiation = _last(df, "forecasted_irradiation")
        kwh                    = _last(df, "kWh")
        irradiation            = _last(df, "irradiation")
        if forecasted_kwh > 0 and forecasted_irradiation > 0 and irradiation > 0:
            abs_diff   = abs(
                ((forecasted_kwh / forecasted_irradiation) - (kwh / irradiation))
                / (forecasted_kwh / forecasted_irradiation) * 100
            )
            
            detail = get_description(mapping, 3, 1, 1)
            append(3, 1, 1,
                   detail["desc"].format(
                       value=round(float(kwh), 2),
                       value1=round(float(irradiation), 2),
                       value2=round(float(forecasted_kwh), 2),
                       value3=round(float(forecasted_irradiation), 2),
                       value4=round(float(abs_diff), 2),
                   ),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] Forecast vs Actual (3,1,1) error: {e}")

    return pd.DataFrame(solar_results)