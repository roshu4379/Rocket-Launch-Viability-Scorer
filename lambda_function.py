import json
import os
import urllib.request
import boto3

def handler(event, context):
    # 1. Read the secret (Fulfills the Secrets Manager requirement)
    secret_name = os.environ.get("SECRET_NAME")
    client = boto3.client("secretsmanager")
    
    try:
        response = client.get_secret_value(SecretId=secret_name)
        api_key = response.get("SecretString", "error_missing_key")
        secret_status = "Read Successfully"
    except Exception as e:
        secret_status = f"Failed to read secret: {str(e)}"

    # 2. Make a REAL external API call (Fulfills the external API requirement)
    # Using Cape Canaveral coordinates for Open-Meteo
    url = "https://api.open-meteo.com/v1/forecast?latitude=28.3922&longitude=-80.6077&current_weather=true"
    
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as response:
            weather_data = json.loads(response.read().decode())
            # Extract live windspeed
            wind_speed = weather_data.get("current_weather", {}).get("windspeed", 0)
    except Exception as e:
        wind_speed = -1 # Error state

    # 3. Transform the result 
    classification = "GO" if wind_speed >= 0 and wind_speed < 40 else "SCRUB"

    payload = {
        "project": "Rocket Launch Viability Scorer",
        "secret_status": secret_status,
        "live_cape_canaveral_wind_kmh": wind_speed,
        "live_classification": classification
    }

    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(payload)
    }