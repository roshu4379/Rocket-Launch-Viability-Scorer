import json
import os
import urllib.request
import urllib.error
import time
import boto3
import concurrent.futures
from datetime import datetime, timedelta, timezone
from decimal import Decimal

def evaluate_launch_weather(lat, lon, target_time_iso):
    """
    Queries Open-Meteo for advanced hourly meteorological data and evaluates 
    it against realistic aerospace Launch Commit Criteria (LCC).
    Uses robust datetime objects to find the nearest forecast hour and handle boundaries.
    """
    weather_url = (
        f"https://api.open-meteo.com/v1/forecast?"
        f"latitude={lat}&longitude={lon}&"
        f"hourly=temperature_2m,relative_humidity_2m,precipitation,"
        f"cloudcover,windspeed_10m,windspeed_500hPa,freezinglevel_height&"
        f"timezone=UTC"
    )
    
    req = urllib.request.Request(weather_url, headers={'User-Agent': 'RocketLaunchViabilityScorer/2.0'})
    
    # Exponential Backoff Configuration
    max_retries = 3
    base_delay = 0.5  # seconds
    weather_data = None
    
    for attempt in range(max_retries + 1):
        try:
            with urllib.request.urlopen(req) as response:
                weather_data = json.loads(response.read().decode())
            break  # Success, exit retry loop
            
        except urllib.error.HTTPError as e:
            if e.code in [429, 500, 502, 503, 504] and attempt < max_retries:
                sleep_time = base_delay * (2 ** attempt)
                print(f"Weather API Error {e.code}. Retrying in {sleep_time}s (Attempt {attempt + 1}/{max_retries})")
                time.sleep(sleep_time)
            else:
                return "ERROR", [f"Weather API HTTP error: {str(e)}"], -1
        except Exception as e:
            if attempt < max_retries:
                sleep_time = base_delay * (2 ** attempt)
                print(f"Network error. Retrying in {sleep_time}s (Attempt {attempt + 1}/{max_retries})")
                time.sleep(sleep_time)
            else:
                return "ERROR", [f"Weather API failed: {str(e)}"], -1

    if not weather_data:
        return "ERROR", ["Weather API failed to return data after retries."], -1

    # 1. Parse target launch timestamp into UTC datetime object
    try:
        clean_target_iso = target_time_iso.replace("Z", "+00:00")
        target_dt = datetime.fromisoformat(clean_target_iso).astimezone(timezone.utc)
    except Exception as e:
        return "ERROR", [f"Invalid target launch time format '{target_time_iso}': {str(e)}"], -1

    hourly_times = weather_data.get('hourly', {}).get('time', [])
    if not hourly_times:
        return "ERROR", ["No hourly forecast data returned from Open-Meteo."], -1

    # 2. Convert forecast timestamps to datetime objects
    forecast_dts = []
    for t_str in hourly_times:
        try:
            # Open-Meteo format: "YYYY-MM-DDTHH:00"
            dt = datetime.strptime(t_str, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
            forecast_dts.append(dt)
        except ValueError:
            continue

    if not forecast_dts:
        return "ERROR", ["Failed to parse hourly forecast timestamps."], -1

    # 3. Find the index of the nearest forecast hour 
    closest_index, closest_dt = min(
        enumerate(forecast_dts),
        key=lambda pair: abs((pair[1] - target_dt).total_seconds())
    )

    # 4. Enforce Boundary Check: explicitly calculate the time difference first
    time_diff = abs(closest_dt - target_dt)
    if time_diff > timedelta(hours=3):
        return "ERROR", [f"Target launch time ({target_time_iso}) is outside forecast boundary."], -1

    time_index = closest_index

    # Extract forecasted metrics for the calculated nearest hour
    temp = weather_data['hourly']['temperature_2m'][time_index]
    humidity = weather_data['hourly']['relative_humidity_2m'][time_index]
    precip = weather_data['hourly']['precipitation'][time_index]
    clouds = weather_data['hourly']['cloudcover'][time_index]
    wind_surface = weather_data['hourly']['windspeed_10m'][time_index]
    wind_upper = weather_data['hourly']['windspeed_500hPa'][time_index]
    freezing_lvl = weather_data['hourly']['freezinglevel_height'][time_index]

    classification = "GO"
    scrub_reasons = []

    # Criteria 1: Temperature & Moisture
    if temp < 2.0 or temp > 38.0:
        scrub_reasons.append(f"Temperature anomaly: {temp}C")
    if humidity > 95.0:
        scrub_reasons.append(f"Humidity saturation risk: {humidity}%")

    # Criteria 2: Cloud Cover & Precipitation
    if precip > 0.0:
        scrub_reasons.append(f"Precipitation violation: {precip}mm")
    if clouds > 80.0 and freezing_lvl < 3000:
        scrub_reasons.append(f"Triggered lightning risk: Thick clouds with low freezing level ({freezing_lvl}m)")

    # Criteria 3: Upper-Level Wind Shear
    if wind_upper > 150.0:
        scrub_reasons.append(f"Upper-level wind shear violation: {wind_upper} km/h")

    # Criteria 4: Surface Winds
    if wind_surface > 40.0:
        scrub_reasons.append(f"Surface wind violation: {wind_surface} km/h")

    if len(scrub_reasons) > 0:
        classification = "SCRUB"

    return classification, scrub_reasons, wind_surface


def process_single_launch(launch, fetch_timestamp, table, sns_client, topic_arn):
    """
    Worker function to process a single launch concurrently.
    Queries previous state to prevent alert fatigue, writes to DB, and sends SNS alerts.
    """
    lat = launch.get("latitude")
    lon = launch.get("longitude")
    
    if lat is None or lon is None:
        return None
        
    # Execute the advanced LCC evaluation
    classification, scrub_reasons, wind_speed = evaluate_launch_weather(lat, lon, launch["window_start"])

    # 1. State-Change Detection: Query the previous status from DynamoDB
    previous_classification = "UNKNOWN"
    try:
        response = table.get_item(
            Key={
                "launch_id": str(launch["launch_id"]),
                "fetch_timestamp": "LATEST_STATUS"
            }
        )
        if "Item" in response:
            previous_classification = response["Item"].get("classification", "UNKNOWN")
    except Exception as e:
        print(f"Failed to query previous state for {launch['launch_id']}: {str(e)}")

    # State Management: Create the item dictionary
    item = {
        "launch_id": str(launch["launch_id"]),
        "fetch_timestamp": "LATEST_STATUS",    
        "last_evaluated_at": fetch_timestamp,  
        "launch_name": launch["launch_name"],
        "pad_name": launch["pad_name"],
        "window_start": launch["window_start"],
        "wind_kmh": Decimal(str(wind_speed)),
        "classification": classification,
        "scrub_reasons": scrub_reasons
    }

    status_changed = False

    # 2. DynamoDB Write with Idempotency (ConditionExpression)
    try:
        table.put_item(
            Item=item,
            ConditionExpression="attribute_not_exists(launch_id) OR classification <> :new_class",
            ExpressionAttributeValues={
                ":new_class": classification
            }
        )
        status_changed = True
    except table.meta.client.exceptions.ConditionalCheckFailedException:
        status_changed = False
    except Exception as e:
        print(f"DynamoDB write failed for {launch['launch_id']}: {str(e)}")
        return None

    # 3. Alerting Fan-Out (ONLY triggers on a strict transition to SCRUB)
    if status_changed and classification == "SCRUB" and previous_classification in ["GO", "UNKNOWN"]:
        try:
            alert_message = {
                "alert_type": "STATE_TRANSITION_SCRUB",
                "previous_state": previous_classification,
                "new_state": classification,
                "launch_name": launch["launch_name"],
                "pad_name": launch["pad_name"],
                "window_start": launch["window_start"],
                "scrub_reasons": scrub_reasons
            }
            sns_client.publish(
                TopicArn=topic_arn,
                Message=json.dumps(alert_message),
                Subject=f"🚀 SCRUB ALERT: {launch['launch_name']} transitioned to SCRUB"
            )
        except Exception as e:
            print(f"SNS publish failed for {launch['launch_id']}: {str(e)}")

    # Convert Decimal back to float for the final JSON payload
    if isinstance(item["wind_kmh"], Decimal):
        item["wind_kmh"] = float(item["wind_kmh"])
    item["status_changed"] = status_changed 
    item["previous_classification"] = previous_classification 
    return item


def handler(event, context):
    # 1. Configuration & Client Setup
    secret_name = os.environ.get("SECRET_NAME")
    table_name = os.environ.get("TABLE_NAME")
    topic_arn = os.environ.get("SNS_TOPIC_ARN")
    
    secrets_client = boto3.client("secretsmanager", region_name="us-east-1")
    dynamodb = boto3.resource("dynamodb", region_name="us-east-1")
    sns_client = boto3.client("sns", region_name="us-east-1")
    table = dynamodb.Table(table_name)
    
    try:
        response = secrets_client.get_secret_value(SecretId=secret_name)
        api_key = response.get("SecretString", "error_missing_key")
        secret_status = "Read Successfully"
    except Exception as e:
        secret_status = f"Failed to read secret: {str(e)}"

    # 2. Fetch upcoming launches from LL2 API (Next 48h)
    ll2_url = "https://ll.thespacedevs.com/2.2.0/launch/upcoming/?limit=15"
    launches_in_window = []
    try:
        req = urllib.request.Request(ll2_url, headers={'User-Agent': 'RocketLaunchViabilityScorer/2.0'})
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            now = datetime.now(timezone.utc)
            cutoff = now + timedelta(hours=48)
            
            for launch in data.get("results", []):
                window_start_str = launch.get("window_start")
                if window_start_str:
                    window_start = datetime.strptime(window_start_str, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                    if now <= window_start <= cutoff:
                        pad = launch.get("pad", {})
                        launches_in_window.append({
                            "launch_id": launch.get("id"),
                            "launch_name": launch.get("name"),
                            "window_start": window_start_str,
                            "pad_name": pad.get("name"),
                            "latitude": pad.get("latitude"),
                            "longitude": pad.get("longitude")
                        })
    except Exception as e:
        print(f"Error fetching from LL2: {str(e)}")

    # 3. Process Weather, Write to State, & Trigger Alerts CONCURRENTLY
    results = []
    fetch_timestamp = datetime.now(timezone.utc).isoformat()
    
    # Use a ThreadPoolExecutor to run API calls in parallel
    with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
        future_to_launch = {
            executor.submit(process_single_launch, launch, fetch_timestamp, table, sns_client, topic_arn): launch 
            for launch in launches_in_window
        }
        
        for future in concurrent.futures.as_completed(future_to_launch):
            try:
                item_result = future.result()
                if item_result:
                    results.append(item_result)
            except Exception as e:
                launch_info = future_to_launch[future]
                print(f"Thread failed for {launch_info.get('launch_id')}: {str(e)}")

    # 6. Return Payload
    payload = {
        "project": "Rocket Launch Viability Scorer v2.0",
        "secret_status": secret_status,
        "scanned_launches_48h": len(launches_in_window),
        "launch_viability_scores": results
    }

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload)
    }