from datetime import datetime, timedelta
import boto3
from apis import login, get_site_db, get_dynamo_form, get_site_list
from data_fetching import getdata, getDataES
from injection import ingest
from config import FORM_ID, AWS_REGION, SQS_URL
from solar import solar
from battery import battery
import pandas as pd

pd.set_option("display.max_columns", None)
pd.set_option("display.width", None)
pd.set_option("display.max_colwidth", None)
pd.set_option("display.max_rows", None)


sqs_client = boto3.client("sqs", region_name=AWS_REGION)
sqs_url    = SQS_URL


def compute_date_windows():
    """
    Returns (week_start, yesterday) as 'YYYY-MM-DD' strings.
      - week_start = yesterday - 7 days
      - yesterday  = today - 1 day
    """
    today      = datetime.now().date()
    yesterday  = today - timedelta(days=1)
    week_start = yesterday - timedelta(days=7)
    return week_start.strftime("%Y-%m-%d"), yesterday.strftime("%Y-%m-%d")


def main():
    # ── Auth + mapping (once, before the site loop) ───────────────────────
    try:
        login_response = login()
        print("[OK] login() succeeded")
    except Exception as e:
        print(f"[ERROR] login() failed: {type(e).__name__}: {e}")
        return
    
    try:
        SITES = get_site_list(login_response)
    except Exception:
        print("get_site_list failed")
        raise

    print(f"Total sites: {len(SITES)}")

    # SITES = ["df6f7a5d-3c51-4527-9d38-3abc396648e2"]


    try:
        status, mapping = get_dynamo_form(FORM_ID, login_response)
        print(f"[OK] get_dynamo_form status={status}")
        if status != 200:
            print(f"[ERROR] get_dynamo_form returned non-200 status: {status}")
            return
    except Exception as e:
        print(f"[ERROR] get_dynamo_form failed: {type(e).__name__}: {e}")
        return

    eligible_count = 0
    skipped_count  = 0

    for sid in SITES:
        print(f"\n{'='*60}")
        print(f"Processing site: {sid}")
        print(f"{'='*60}")

        # ── Site DB ───────────────────────────────────────────────────
        try:
            site_json = get_site_db(sid, login_response)
            if not site_json:
                print(f"[SKIP] get_site_db returned empty for {sid}")
                skipped_count += 1
                continue
            print(f"[OK] site_json fetched — keys: {list(site_json.keys())}")
        except Exception as e:
            print(f"[ERROR] get_site_db failed: {type(e).__name__}: {e}")
            skipped_count += 1
            continue

        # ── Eligibility check: only Contract.Type == 0 ──────────────────
        contract_raw  = site_json.get("Contract", {}).get("Type")
        try:
            contract_type = int(contract_raw)
        except (TypeError, ValueError):
            contract_type = None

        if contract_type != 0:
            print(f"[SKIP] site {sid} ineligible — Contract.Type={contract_raw!r}")
            skipped_count += 1
            continue

        eligible_count += 1

        raw_index       = site_json.get("index_pattern", "") + "2019"
        PROCESSED_INDEX = "site-cni-processed-data"
        print(f"[INFO] raw_index={raw_index}  processed_index={PROCESSED_INDEX}")

        week_start, yesterday = compute_date_windows()
        print(f"[INFO] Processed window : {week_start}  →  {yesterday}")
        print(f"[INFO] Raw window       : {yesterday}  →  {yesterday}")
        print(f"[INFO] Forecast window  : {yesterday}  →  {yesterday}")

        # ── Data fetching ─────────────────────────────────────────────
        try:
            df_processed = getdata(PROCESSED_INDEX, week_start, yesterday, sid)
            print(f"[OK] df_processed shape={getattr(df_processed, 'shape', 'N/A')}")
        except Exception as e:
            print(f"[ERROR] getdata(processed) failed: {type(e).__name__}: {e}")
            continue

        try:
            df_raw = getDataES(raw_index, yesterday, yesterday)
            print(f"[OK] df_raw shape={getattr(df_raw, 'shape', 'N/A')}")
        except Exception as e:
            print(f"[ERROR] getDataES(raw) failed: {type(e).__name__}: {e}")
            continue

        try:
            df_forecast = getdata(PROCESSED_INDEX, yesterday, yesterday, sid)
            print(f"[OK] df_forecast shape={getattr(df_forecast, 'shape', 'N/A')}")
        except Exception as e:
            print(f"[ERROR] getdata(forecast) failed: {type(e).__name__}: {e}")
            continue

        if df_processed is False or df_raw is False or df_forecast is False:
            print(
                f"[ERROR] One or more fetches returned False — "
                f"processed={df_processed is not False}, "
                f"raw={df_raw is not False}, "
                f"forecast={df_forecast is not False}"
            )
            continue

        # ── Determine which device types this site actually has ────────
        site_source  = site_json.get("site_source", {}) or {}
        source_types = set(site_source.values()) if site_source else set()
        has_solar    = "solar" in source_types
        has_bess     = "bess" in source_types

        print(f"[INFO] site_source types={source_types}  has_solar={has_solar}  has_bess={has_bess}")

        # ── Insights ──────────────────────────────────────────────────
        try:
            insight_frames = []

            if has_solar:
                insights = solar(df_processed, df_raw, df_forecast, login_response, mapping, sid)
                insight_frames.append(insights)
            else:
                print(f"[SKIP] site {sid} has no solar source — skipping solar()")

            if has_bess:
                insights2 = battery(df_processed, df_raw, login_response, mapping, sid)
                insight_frames.append(insights2)
            else:
                print(f"[SKIP] site {sid} has no bess source — skipping battery()")

            if not insight_frames:
                print(f"[SKIP] site {sid} has neither solar nor bess — nothing to ingest")
                continue

            combined_insights = pd.concat(insight_frames, ignore_index=True)

            print(f"[OK] model returned {len(combined_insights)} insight(s)")

            combined_insights.to_csv("combined_insights.csv", index=True)

            # print(insights)

        except Exception as e:
            import traceback
            print(f"[ERROR] solar()/battery() failed: {type(e).__name__}: {e}")
            traceback.print_exc()
            continue

        # ── Injection ─────────────────────────────────────────────────
        ingest(sid, combined_insights, sqs_client, sqs_url)

    print(f"\n[SUMMARY] eligible={eligible_count}  skipped={skipped_count}  total={len(SITES)}")


if __name__ == "__main__":
    main()