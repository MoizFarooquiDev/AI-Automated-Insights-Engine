"""
Solar insights generator.

Reads processed + raw + forecast dataframes for a site and emits a list of
insight records covering Under Performance, Fault, SLA, and Forecast families.
"""

import re
import statistics

import pandas as pd


from apis import get_site_db, get_std_details, get_description
from thresholds import BATTERY as T


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


def battery(df_processed, df_raw, login_response, mapping, site_id):
    batt_results = []

    # ── Site Setup ────────────────────────────────────────────────────────────
    try:
        site_json    = get_site_db(site_id, login_response)
        name         = site_json.get("siteInfo", {}).get("siteName", "")
        system_size  = site_json.get("siteInfo", {}).get("siteSolarPower", 0)
        temp_model   = site_json.get("siteInfo", {}).get("TemperatureModel", "open_rack_glass_glass")
        gamma_pdc    = site_json.get("siteInfo", {}).get("gamma_pdc", -0.0035)
        installation = site_json.get("InstallationModelization", {}).get("Inverter", {})
        RACK_no      = site_json.get("Reports", {}).get("Chunk_SOC_Delta", {}).get("RACK_no", 1)
        CHUNK_no     = site_json.get("Reports", {}).get("Chunk_SOC_Delta", {}).get("CHUNK_no", 4)
        pcs  = site_json.get("siteInfo", {}).get("infoWidget", {}).get("PCS Quantity", 0)
        print(pcs)
        bess_ac_raw  = site_json.get("siteInfo", {}).get("infoWidget", {}).get("BESS AC", 0)
        bess_ac_kw   = float(str(bess_ac_raw).replace("MW", "").strip()) * 1000

        df  = df_processed.copy()
        _, _, rawMapping = get_std_details(site_json)

        # ── df2: BESS_P minute-level data ─────────────────────────────────────
        req_fields_df2 = ["TIMESTAMP", "BESS_P"]
        df2            = df_raw.copy().reset_index()
        df2.columns    = [rawMapping.get(c, c) for c in df2.columns]
        missing_cols   = list(set(req_fields_df2) - set(df2.columns))
        df2            = pd.concat([df2, pd.DataFrame(columns=missing_cols)], axis=1)[req_fields_df2]
        df2["TIMESTAMP"] = pd.to_datetime(df2["TIMESTAMP"], errors="coerce")
        df2.set_index("TIMESTAMP", inplace=True)
        df2 = df2[df2.index.notna()].resample("min").mean().reset_index()
        df2.dropna(subset=["BESS_P"], how="all", inplace=True)

        # ── data: full raw mapped df with TIMESTAMP for BMS insites ───────────
        soc_cols      = [f"BMS{i+1}_RK{j+1}SOC"      for i in range(CHUNK_no) for j in range(RACK_no)]
        max_volt_cols = [f"BMS_0{i+1}_MAXCELLVOLTS"   for i in range(CHUNK_no)]
        min_volt_cols = [f"BMS_0{i+1}_MINCELLVOLTS"   for i in range(CHUNK_no)]
        max_freq_cols = [f"PCS_0{i+1}_FREQUENCY"   for i in range(pcs)]
        min_freq_cols = [f"PCS_0{i+1}_FREQUENCY"   for i in range(pcs)]
        max_acvolt_cols = [f"PCS_0{i+1}_ACVOLTAGE"   for i in range(pcs)]
        min_acvolt_cols = [f"PCS_0{i+1}_ACVOLTAGE"   for i in range(pcs)]
        all_req_cols  = list(set(soc_cols + max_volt_cols + min_volt_cols + max_freq_cols + min_freq_cols + min_freq_cols + min_acvolt_cols))

        data         = df_raw.copy()
        data.columns = list(map(rawMapping.get, data.columns))
        data         = data.ffill().bfill().reset_index(drop=False)
        for ts_col in ["@timestamp", "index", "level_0"]:
            if ts_col in data.columns:
                data.rename(columns={ts_col: "TIMESTAMP"}, inplace=True)
                break
        data["TIMESTAMP"] = pd.to_datetime(data["TIMESTAMP"], utc=True)
        missing           = list(set(all_req_cols) - set(data.columns))
        data              = pd.concat([data, pd.DataFrame(columns=missing)], axis=1)


    except Exception as e:
        print(f"[{site_id}] Site setup error: {e}")
        return []

    def append(cat, sub, det, desc, sev, opp_cost=0):
        batt_results.append({
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

    # ── 0,3,0  Low Plant Availability ────────────────────────────────────────
    try:
        uptime       = _last(df, "BESS_Uptime")
        target_value = T["low_plant_availability"]["uptime_min_pct"]
        if target_value > uptime:
            detail = get_description(mapping, 0, 3, 0)
            append(0, 3, 0,
                   detail["desc"].format(value=round(float(uptime), 2), value1=round(float(target_value), 2)),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] Low Plant Availability (0,3,0) error: {e}")

    # ── 5,3,0  High Cell Temp ─────────────────────────────────────────────────
    try:
        cell_temp    = _last(df, "REFLEX_Batt_Max_Temp")
        target_value = T["high_cell_temp"]["max_temp_c"]
        if cell_temp > target_value:
            detail = get_description(mapping, 5, 3, 0)
            append(5, 3, 0,
                   detail["desc"].format(value=round(float(cell_temp), 2), value1=round(float(target_value), 2)),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] High Cell Temp (5,3,0) error: {e}")

    # ── 5,3,1  SOC Imbalance ──────────────────────────────────────────────────
    try:
        soc_data  = data[soc_cols].tail(1)
        max_delta = 0.0
        max_chunk = None
        chunk_means = {}

        for i in range(CHUNK_no):
            rack_cols       = [f"BMS{i+1}_RK{j+1}SOC" for j in range(RACK_no)]
            valid_rack_cols = [
                col for col in rack_cols
                if col in soc_data.columns
                and soc_data.iloc[0][col] is not None
                and not pd.isna(soc_data.iloc[0][col])
                and soc_data.iloc[0][col] != 0
            ]
            if valid_rack_cols:
                delta = float(soc_data[valid_rack_cols].max(axis=1).iloc[0] - soc_data[valid_rack_cols].min(axis=1).iloc[0])
                chunk_means[f"CHUNK{i+1}"] = delta
                if delta > max_delta:
                    max_delta = delta
                    max_chunk = f"CHUNK{i+1}"
            else:
                chunk_means[f"CHUNK{i+1}"] = 0.0

        if max_delta > T["soc_imbalance"]["max_delta_pct"] and max_chunk:
            detail = get_description(mapping, 5, 3, 1)
            append(5, 3, 1,
                   detail["desc"].format(value=round(max_delta, 2), value1=max_chunk),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] SOC Imbalance (5,3,1) error: {e}")

    # ── 5,3,2  High SOC ───────────────────────────────────────────────────────
    try:
        max_soc      = _last(df, "Batt_Rack_Max_SOC")
        target_value = T["high_soc"]["max_soc_pct"]
        if max_soc >= target_value:
            detail = get_description(mapping, 5, 3, 2)
            append(5, 3, 2,
                   detail["desc"].format(value=round(float(max_soc), 2), value1=round(float(target_value), 2)),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] High SOC (5,3,2) error: {e}")

    # ── 5,3,3  Low SOC ────────────────────────────────────────────────────────
    try:
        min_soc      = _last(df, "Batt_Rack_Min_SOC")
        target_value = T["low_soc"]["min_soc_pct"]
        if min_soc <= target_value:
            detail = get_description(mapping, 5, 3, 3)
            append(5, 3, 3,
                   detail["desc"].format(value=round(float(min_soc), 2), value1=round(float(target_value), 2)),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] Low SOC (5,3,3) error: {e}")

    # ── 5,3,4  High PCS Temp ──────────────────────────────────────────────────
    try:
        pcs_temp     = _last(df, "REFLEX_PCS_Max_Temp")
        target_value = T["high_pcs_temp"]["max_temp_c"]
        if pcs_temp > target_value:
            detail = get_description(mapping, 5, 3, 4)
            append(5, 3, 4,
                   detail["desc"].format(value=round(float(pcs_temp), 2), value1=round(float(target_value), 2)),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] High PCS Temp (5,3,4) error: {e}")

    # ── 5,3,5  BESS Full Utilization ─────────────────────────────────────────
    try:
        tolerance          = bess_ac_kw * T["bess_full_utilization"]["tolerance_pct"]
        bess_data          = df2.copy()
        bess_data["is_full"]    = bess_data["BESS_P"].abs() >= (bess_ac_kw - tolerance)
        bess_data["full_group"] = (bess_data["is_full"] != bess_data["is_full"].shift()).cumsum()
        full_blocks             = bess_data[bess_data["is_full"]].groupby("full_group")

        events = []
        for _, block in full_blocks:
            start_str = block["TIMESTAMP"].iloc[0].strftime("%I:%M %p").lstrip("0")
            end_str   = block["TIMESTAMP"].iloc[-1].strftime("%I:%M %p").lstrip("0")
            events.append(f"{start_str} to {end_str}")

        if events:
            detail = get_description(mapping, 5, 3, 5)
            append(5, 3, 5,
                   detail["desc"].format(value=_human_list(events)),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] BESS Full Utilization (5,3,5) error: {e}")

    # ── 5,3,6  BESS Over Utilization ─────────────────────────────────────────
    try:
        bess_data          = df2.copy()
        bess_data["is_over"]    = bess_data["BESS_P"].abs() > bess_ac_kw
        bess_data["over_group"] = (bess_data["is_over"] != bess_data["is_over"].shift()).cumsum()
        over_blocks             = bess_data[bess_data["is_over"]].groupby("over_group")

        events = []
        for _, block in over_blocks:
            start_str = block["TIMESTAMP"].iloc[0].strftime("%I:%M %p").lstrip("0")
            end_str   = block["TIMESTAMP"].iloc[-1].strftime("%I:%M %p").lstrip("0")
            max_power = round(block["BESS_P"].abs().max(), 2)
            max_pct   = round((max_power / bess_ac_kw) * 100, 1)
            events.append(f"{start_str} to {end_str} (peak: {max_power} kW / {max_pct}%)")

        if events:
            detail = get_description(mapping, 5, 3, 6)
            append(5, 3, 6,
                   detail["desc"].format(value=_human_list(events)),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] BESS Over Utilization (5,3,6) error: {e}")

    # ── 5,3,7  BESS Max Cell Voltage ─────────────────────────────────────────
    try:
        target_max_v = T["bess_max_cell_voltage"]["max_voltage_v"]
        bms_data     = data[["TIMESTAMP"] + max_volt_cols].copy()

        breached = []
        for i in range(CHUNK_no):
            col     = f"BMS_0{i+1}_MAXCELLVOLTS"
            max_val = bms_data[col].max()
            if pd.notna(max_val) and max_val > target_max_v:
                max_time_str = bms_data.loc[bms_data[col].idxmax(), "TIMESTAMP"].strftime("%I:%M %p").lstrip("0")
                breached.append((f"BMS{i+1}", round(float(max_val), 4), max_time_str))

        if breached:
            detail = get_description(mapping, 5, 3, 7)
            append(5, 3, 7,
                   detail["desc"].format(
                       value=_human_list([b[0] for b in breached]),
                       value1=", ".join([str(b[1]) for b in breached]),
                       value2=round(float(target_max_v), 4),
                       value3=", ".join([b[2] for b in breached])
                   ),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] BESS Max Cell Voltage (5,3,7) error: {e}")

    # ── 5,3,8  BESS Min Cell Voltage ─────────────────────────────────────────
    try:
        target_min_v = T["bess_min_cell_voltage"]["min_voltage_v"]
        bms_data     = data[["TIMESTAMP"] + min_volt_cols].copy()

        breached = []
        for i in range(CHUNK_no):
            col     = f"BMS_0{i+1}_MINCELLVOLTS"
            min_val = bms_data[col].min()
            if pd.notna(min_val) and min_val > 0 and min_val < target_min_v:
                min_time_str = bms_data.loc[bms_data[col].idxmin(), "TIMESTAMP"].strftime("%I:%M %p").lstrip("0")
                breached.append((f"BMS{i+1}", round(float(min_val), 4), min_time_str))

        if breached:
            detail = get_description(mapping, 5, 3, 8)
            append(5, 3, 8,
                   detail["desc"].format(
                       value=_human_list([b[0] for b in breached]),
                       value1=", ".join([str(b[1]) for b in breached]),
                       value2=round(float(target_min_v), 4),
                       value3=", ".join([b[2] for b in breached])
                   ),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] BESS Min Cell Voltage (5,3,8) error: {e}")

    # ── 5,3,9  Max Frequency ─────────────────────────────────────────
    try:
        target_max_v = T["max_frequency"]["max_frequency_hz"]
        pcs_data     = data[["TIMESTAMP"] + max_freq_cols].copy()

        breached = []
        for i in range(pcs):
            col     = f"PCS_0{i+1}_FREQUENCY"
            max_val = pcs_data[col].max()
            if pd.notna(max_val) and max_val > target_max_v:
                max_time_str = pcs_data.loc[pcs_data[col].idxmax(), "TIMESTAMP"].strftime("%I:%M %p").lstrip("0")
                breached.append((f"PCS{i+1}", round(float(max_val), 4), max_time_str))

        if breached:
            detail = get_description(mapping, 5, 3, 9)
            append(5, 3, 9,
                   detail["desc"].format(
                       value=_human_list([b[0] for b in breached]),
                       value1=", ".join([str(b[1]) for b in breached]),
                       value2=round(float(target_max_v), 4),
                       value3=", ".join([b[2] for b in breached])
                   ),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] 5,3,9  Max Frequency error: {e}")

    # ── 5,3,10   Min Frequency ─────────────────────────────────────────
    try:
        target_min_v = T["min_frequency"]["min_frequency_hz"]
        pcs_data     = data[["TIMESTAMP"] + min_freq_cols].copy()

        breached = []
        for i in range(pcs):
            col     = f"PCS_0{i+1}_FREQUENCY"
            min_val = pcs_data[col].min()
            if pd.notna(min_val) and min_val > 0 and min_val < target_min_v:
                min_time_str = pcs_data.loc[pcs_data[col].idxmin(), "TIMESTAMP"].strftime("%I:%M %p").lstrip("0")
                breached.append((f"PCS{i+1}", round(float(min_val), 4), min_time_str))

        if breached:
            detail = get_description(mapping, 5, 3, 10)
            append(5, 3, 10,
                   detail["desc"].format(
                       value=_human_list([b[0] for b in breached]),
                       value1=", ".join([str(b[1]) for b in breached]),
                       value2=round(float(target_min_v), 4),
                       value3=", ".join([b[2] for b in breached])
                   ),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] 5,3,10   Min Frequency error: {e}")

    # ── 5,3,11  High AC Voltage ─────────────────────────────────────────
    try:
        target_max_v = T["high_ac_voltage"]["max_voltage_v"]
        pcs_data     = data[["TIMESTAMP"] + max_acvolt_cols].copy()

        breached = []
        for i in range(pcs):
            col     = f"PCS_0{i+1}_ACVOLTAGE"
            max_val = pcs_data[col].max()
            if pd.notna(max_val) and max_val > target_max_v:
                max_time_str = pcs_data.loc[pcs_data[col].idxmax(), "TIMESTAMP"].strftime("%I:%M %p").lstrip("0")
                breached.append((f"PCS{i+1}", round(float(max_val), 4), max_time_str))

        if breached:
            detail = get_description(mapping, 5, 3, 11)
            append(5, 3, 11,
                   detail["desc"].format(
                       value=_human_list([b[0] for b in breached]),
                       value1=", ".join([str(b[1]) for b in breached]),
                       value2=round(float(target_max_v), 4),
                       value3=", ".join([b[2] for b in breached])
                   ),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] 5,3,11  Max AC Volt error: {e}")

    # ── 5,3,12  Low AC Voltage ─────────────────────────────────────────
    try:
        target_min_v = T["low_ac_voltage"]["min_voltage_v"]
        pcs_data     = data[["TIMESTAMP"] + min_acvolt_cols].copy()

        breached = []
        for i in range(pcs):
            col     = f"PCS_0{i+1}_ACVOLTAGE"
            min_val = pcs_data[col].min()
            if pd.notna(min_val) and min_val > 0 and min_val < target_min_v:
                min_time_str = pcs_data.loc[pcs_data[col].idxmin(), "TIMESTAMP"].strftime("%I:%M %p").lstrip("0")
                breached.append((f"PCS{i+1}", round(float(min_val), 4), min_time_str))

        if breached:
            detail = get_description(mapping, 5, 3, 12)
            append(5, 3, 12,
                   detail["desc"].format(
                       value=_human_list([b[0] for b in breached]),
                       value1=", ".join([str(b[1]) for b in breached]),
                       value2=round(float(target_min_v), 4),
                       value3=", ".join([b[2] for b in breached])
                   ),
                   detail["Sev"])
    except Exception as e:
        print(f"[{site_id}] 5,3,12   Low AC Voltage: {e}")

    return pd.DataFrame(batt_results)