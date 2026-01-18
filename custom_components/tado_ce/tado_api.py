#!/usr/bin/env python3
"""
Tado API Client - All-in-one script for Home Assistant
Handles authentication, token refresh, API calls, and rate limit tracking.

Usage:
  python3 tado_api.py sync          # Sync zone data + rate limits
  python3 tado_api.py auth          # Interactive device authorization
  python3 tado_api.py status        # Show current status
  
Config file: /tmp/tado_ce_config.json
Output files: /tmp/tado_zones.json, /tmp/tado_ratelimit.json
"""

import json
import sys
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError

# Configuration - use persistent storage
DATA_DIR = Path("/config/custom_components/tado_ce/data")
DATA_DIR.mkdir(exist_ok=True)

CONFIG_FILE = DATA_DIR / "config.json"
ZONES_FILE = DATA_DIR / "zones.json"
ZONES_INFO_FILE = DATA_DIR / "zones_info.json"
RATELIMIT_FILE = DATA_DIR / "ratelimit.json"
WEATHER_FILE = DATA_DIR / "weather.json"
MOBILE_DEVICES_FILE = DATA_DIR / "mobile_devices.json"
LOG_FILE = DATA_DIR / "api.log"

CLIENT_ID = "1bb50063-6b0c-4d11-bd99-387f4a91cc46"

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
log = logging.getLogger(__name__)


class TadoAPIError(Exception):
    """Custom exception for Tado API errors."""
    pass


class TadoClient:
    """Tado API client with automatic token management."""
    
    def __init__(self):
        self.config = self._load_config()
        self.access_token = None
        self.rate_limit = {}
    
    def _load_config(self) -> dict:
        """Load config from file."""
        if not CONFIG_FILE.exists():
            return {"home_id": None, "refresh_token": None}
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception as e:
            log.warning(f"Failed to load config: {e}")
            return {"home_id": None, "refresh_token": None}
    
    def _save_config(self):
        """Save config to file."""
        try:
            with open(CONFIG_FILE, 'w') as f:
                json.dump(self.config, f, indent=2)
            log.debug("Config saved")
        except Exception as e:
            log.error(f"Failed to save config: {e}")
    
    def _save_ratelimit(self, status: str = "ok", error: str = None):
        """Save rate limit info to file."""
        from datetime import timezone
        now = datetime.now()
        now_utc = datetime.now(timezone.utc)
        
        # Load previous rate limit data to detect reset
        prev_data = {}
        if RATELIMIT_FILE.exists():
            try:
                with open(RATELIMIT_FILE) as f:
                    prev_data = json.load(f)
            except:
                pass
        
        # Calculate human-readable reset time
        reset_seconds = self.rate_limit.get("reset_seconds")
        reset_human = "unknown"
        reset_at = "unknown"
        
        # Get previous remaining and last known reset time
        prev_remaining = prev_data.get("remaining")
        last_reset_utc = prev_data.get("last_reset_utc")  # ISO format string - preserve it!
        current_remaining = self.rate_limit.get("remaining")
        
        # Detect if rate limit has reset (remaining increased significantly)
        if prev_remaining is not None and current_remaining is not None:
            if current_remaining > prev_remaining + 100:  # Reset detected
                # Record this reset time
                last_reset_utc = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                log.info(f"Rate limit reset detected at {last_reset_utc}")
        
        # If we don't have last_reset_utc yet, we can't calculate reset time
        # It will be set automatically when we detect a reset
        
        # Calculate reset time based on last known reset (rolling 24h window)
        if last_reset_utc and not reset_seconds:
            try:
                last_reset = datetime.fromisoformat(last_reset_utc.replace('Z', '+00:00'))
                next_reset = last_reset + timedelta(hours=24)
                reset_seconds = int((next_reset - now_utc).total_seconds())
                if reset_seconds < 0:
                    # Already past, estimate next one
                    reset_seconds = reset_seconds % 86400  # Wrap around
                    if reset_seconds < 0:
                        reset_seconds += 86400
            except Exception as e:
                log.debug(f"Failed to calculate reset from last_reset_utc: {e}")
        
        # Fallback: if still no reset_seconds, show "unknown" but don't guess
        if reset_seconds and reset_seconds > 0:
            hours = reset_seconds // 3600
            mins = (reset_seconds % 3600) // 60
            reset_human = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
            reset_at = (now + timedelta(seconds=reset_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
        
        # Calculate percentage
        limit = self.rate_limit.get("limit")
        remaining = self.rate_limit.get("remaining")
        used = None
        percentage = None
        
        if limit and remaining is not None:
            used = limit - remaining
            percentage = round(used * 100 / limit) if limit > 0 else 0
            if remaining == 0:
                status = "rate_limited"
            elif percentage > 80:
                status = "warning"
        
        data = {
            "limit": limit,
            "remaining": remaining,
            "reset_seconds": reset_seconds if reset_seconds and reset_seconds > 0 else None,
            "reset_human": reset_human,
            "reset_at": reset_at,
            "used": used,
            "percentage_used": percentage,
            "last_updated": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "last_reset_utc": last_reset_utc,  # Track last known reset time
            "status": status,
            "error": error
        }
        
        try:
            with open(RATELIMIT_FILE, 'w') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            log.error(f"Failed to save rate limit: {e}")
        
        return data
    
    def _parse_ratelimit_headers(self, headers: dict):
        """Parse Tado rate limit headers."""
        # Tado format:
        #   ratelimit-policy: "perday";q=5000;w=86400
        #   ratelimit: "perday";r=0;t=5904
        
        policy = headers.get("ratelimit-policy", "")
        ratelimit = headers.get("ratelimit", "")
        
        # Parse limit from policy (q=)
        if "q=" in policy:
            try:
                self.rate_limit["limit"] = int(policy.split("q=")[1].split(";")[0])
            except (ValueError, IndexError):
                pass
        
        # Parse remaining (r=) and reset (t=) from ratelimit
        if "r=" in ratelimit:
            try:
                self.rate_limit["remaining"] = int(ratelimit.split("r=")[1].split(";")[0])
            except (ValueError, IndexError):
                pass
        
        if "t=" in ratelimit:
            try:
                self.rate_limit["reset_seconds"] = int(ratelimit.split("t=")[1].split(";")[0])
            except (ValueError, IndexError):
                pass
    
    def _http_request(self, url: str, method: str = "GET", data: dict = None, 
                      headers: dict = None, parse_ratelimit: bool = True) -> tuple:
        """Make HTTP request and return (response_data, response_headers)."""
        req_headers = headers or {}
        body = None
        
        if data:
            body = urlencode(data).encode('utf-8')
            req_headers.setdefault('Content-Type', 'application/x-www-form-urlencoded')
        
        req = Request(url, data=body, headers=req_headers, method=method)
        
        try:
            with urlopen(req, timeout=30) as response:
                resp_headers = {k.lower(): v for k, v in response.getheaders()}
                resp_body = response.read().decode('utf-8')
                resp_data = json.loads(resp_body) if resp_body else {}
                
                if parse_ratelimit:
                    self._parse_ratelimit_headers(resp_headers)
                
                return resp_data, resp_headers
                
        except HTTPError as e:
            resp_headers = {k.lower(): v for k, v in e.headers.items()}
            if parse_ratelimit:
                self._parse_ratelimit_headers(resp_headers)
            
            try:
                error_body = json.loads(e.read().decode('utf-8'))
            except:
                error_body = {"error": str(e)}
            
            raise TadoAPIError(f"HTTP {e.code}: {error_body}")
        
        except URLError as e:
            raise TadoAPIError(f"Network error: {e.reason}")
    
    def refresh_access_token(self) -> bool:
        """Refresh access token using refresh token."""
        refresh_token = self.config.get("refresh_token")
        if not refresh_token:
            log.error("No refresh token available. Run 'auth' first.")
            return False
        
        log.info("Refreshing access token...")
        
        try:
            data, _ = self._http_request(
                "https://login.tado.com/oauth2/token",
                method="POST",
                data={
                    "client_id": CLIENT_ID,
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token
                },
                parse_ratelimit=False
            )
            
            self.access_token = data.get("access_token")
            new_refresh = data.get("refresh_token")
            
            if not self.access_token:
                log.error("No access token in response")
                return False
            
            # CRITICAL: Save new refresh token immediately
            if new_refresh and new_refresh != refresh_token:
                self.config["refresh_token"] = new_refresh
                self._save_config()
                log.info("Refresh token rotated and saved")
            
            log.info("Access token refreshed successfully")
            return True
            
        except TadoAPIError as e:
            log.error(f"Token refresh failed: {e}")
            if "invalid_grant" in str(e):
                log.error("Refresh token expired. Run 'auth' to re-authenticate.")
                self.config["refresh_token"] = None
                self._save_config()
            return False
    
    def device_auth(self) -> bool:
        """Perform device authorization flow."""
        log.info("Starting device authorization...")
        
        try:
            # Step 1: Request device code
            data, _ = self._http_request(
                "https://login.tado.com/oauth2/device_authorize",
                method="POST",
                data={
                    "client_id": CLIENT_ID,
                    "scope": "home.user offline_access"
                },
                parse_ratelimit=False
            )
            
            device_code = data.get("device_code")
            user_code = data.get("user_code")
            verify_url = data.get("verification_uri_complete")
            interval = data.get("interval", 5)
            expires_in = data.get("expires_in", 300)
            
            if not device_code:
                log.error("Failed to get device code")
                return False
            
            print("\n" + "=" * 50)
            print("Please visit this URL to authorize:")
            print(verify_url)
            print(f"\nOr go to: https://login.tado.com/device")
            print(f"And enter code: {user_code}")
            print("=" * 50)
            print(f"\nWaiting for authorization (expires in {expires_in}s)...")
            
            # Step 2: Poll for token
            max_attempts = expires_in // interval
            for attempt in range(max_attempts):
                time.sleep(interval)
                
                try:
                    token_data, _ = self._http_request(
                        "https://login.tado.com/oauth2/token",
                        method="POST",
                        data={
                            "client_id": CLIENT_ID,
                            "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                            "device_code": device_code
                        },
                        parse_ratelimit=False
                    )
                    
                    self.access_token = token_data.get("access_token")
                    refresh_token = token_data.get("refresh_token")
                    
                    if self.access_token and refresh_token:
                        self.config["refresh_token"] = refresh_token
                        self._save_config()
                        log.info("Authorization successful!")
                        print("\n✓ Authorization complete!")
                        return True
                        
                except TadoAPIError as e:
                    error_str = str(e)
                    if "authorization_pending" in error_str:
                        print(".", end="", flush=True)
                    elif "slow_down" in error_str:
                        interval += 5
                        print("s", end="", flush=True)
                    else:
                        log.error(f"Auth error: {e}")
                        return False
            
            log.error("Authorization timed out")
            return False
            
        except TadoAPIError as e:
            log.error(f"Device auth failed: {e}")
            return False
    
    def fetch_home_id(self) -> str:
        """Fetch home ID from Tado API using /me endpoint."""
        if not self.access_token:
            if not self.refresh_access_token():
                raise TadoAPIError("Not authenticated")
        
        log.info("Fetching home ID from API...")
        
        try:
            data, _ = self._http_request(
                "https://my.tado.com/api/v2/me",
                headers={"Authorization": f"Bearer {self.access_token}"},
                parse_ratelimit=False
            )
            
            homes = data.get("homes", [])
            if not homes:
                raise TadoAPIError("No homes found for this account")
            
            # Use the first home (most users have only one)
            home_id = str(homes[0].get("id"))
            home_name = homes[0].get("name", "Unknown")
            
            log.info(f"Found home: {home_name} (ID: {home_id})")
            
            if len(homes) > 1:
                log.warning(f"Multiple homes found ({len(homes)}), using first one: {home_name}")
                for h in homes:
                    log.info(f"  - {h.get('name')} (ID: {h.get('id')})")
            
            return home_id
            
        except TadoAPIError as e:
            if "401" in str(e):
                log.info("Token expired, refreshing...")
                if self.refresh_access_token():
                    return self.fetch_home_id()
            raise

    def api_call(self, endpoint: str) -> dict:
        """Make authenticated API call."""
        if not self.access_token:
            if not self.refresh_access_token():
                raise TadoAPIError("Not authenticated")
        
        home_id = self.config.get("home_id")
        if not home_id:
            # Auto-fetch home ID if not set
            home_id = self.fetch_home_id()
            self.config["home_id"] = home_id
            self._save_config()
            log.info(f"Home ID saved to config: {home_id}")
        
        url = f"https://my.tado.com/api/v2/homes/{home_id}/{endpoint}"
        
        try:
            data, _ = self._http_request(
                url,
                headers={"Authorization": f"Bearer {self.access_token}"}
            )
            return data
            
        except TadoAPIError as e:
            if "401" in str(e) or "403" in str(e):
                # Token might be expired, try refresh
                log.info("Token expired, refreshing...")
                if self.refresh_access_token():
                    data, _ = self._http_request(
                        url,
                        headers={"Authorization": f"Bearer {self.access_token}"}
                    )
                    return data
            raise
    
    def sync(self, quick: bool = False) -> bool:
        """Sync zone data and rate limits.
        
        Args:
            quick: If True, only sync zoneStates and weather (2 calls).
                   If False, sync all data including zones and mobileDevices (4 calls).
        """
        sync_type = "quick" if quick else "full"
        log.info(f"Starting {sync_type} sync...")
        
        try:
            # Always fetch zone states (most important)
            zones_data = self.api_call("zoneStates")
            with open(ZONES_FILE, 'w') as f:
                json.dump(zones_data, f, indent=2)
            log.info(f"Zone states saved ({len(zones_data.get('zoneStates', {}))} zones)")
            
            # Always fetch weather (changes frequently)
            weather_data = self.api_call("weather")
            with open(WEATHER_FILE, 'w') as f:
                json.dump(weather_data, f, indent=2)
            log.info("Weather data saved")
            
            if not quick:
                # Full sync: also fetch zone info and mobile devices
                zones_info = self.api_call("zones")
                with open(ZONES_INFO_FILE, 'w') as f:
                    json.dump(zones_info, f, indent=2)
                log.info(f"Zone info saved ({len(zones_info)} zones)")
                
                mobile_data = self.api_call("mobileDevices")
                with open(MOBILE_DEVICES_FILE, 'w') as f:
                    json.dump(mobile_data, f, indent=2)
                log.info(f"Mobile devices saved ({len(mobile_data)} devices)")
            
            # Save rate limit info
            rl = self._save_ratelimit()
            log.info(f"Rate limit: {rl['used']}/{rl['limit']} ({rl['percentage_used']}%), "
                    f"reset in {rl['reset_human']} at {rl['reset_at']}")
            
            return True
            
        except TadoAPIError as e:
            log.error(f"Sync failed: {e}")
            if "429" in str(e):
                self._save_ratelimit(status="rate_limited", error="rate_limited")
            else:
                self._save_ratelimit(status="error", error=str(e))
            return False
    
    def status(self):
        """Show current status."""
        print("\n=== Tado API Status ===")
        
        # Config
        print(f"\nConfig file: {CONFIG_FILE}")
        print(f"  Home ID: {self.config.get('home_id', 'not set')}")
        print(f"  Refresh token: {'set' if self.config.get('refresh_token') else 'not set'}")
        
        # Rate limit
        if RATELIMIT_FILE.exists():
            with open(RATELIMIT_FILE) as f:
                rl = json.load(f)
            print(f"\nRate limit:")
            print(f"  Used: {rl.get('used')}/{rl.get('limit')} ({rl.get('percentage_used')}%)")
            print(f"  Remaining: {rl.get('remaining')}")
            print(f"  Reset: {rl.get('reset_human')} (at {rl.get('reset_at')})")
            print(f"  Status: {rl.get('status')}")
            print(f"  Last updated: {rl.get('last_updated')}")
        else:
            print("\nRate limit: no data (run sync first)")
        
        # Zones
        if ZONES_FILE.exists():
            try:
                with open(ZONES_FILE) as f:
                    zones = json.load(f)
                print(f"\nZones: {len(zones.get('zoneStates', {}))} zones loaded")
            except json.JSONDecodeError:
                print("\nZones: invalid data (API may have returned error)")
        else:
            print("\nZones: no data (run sync first)")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)
    
    command = sys.argv[1].lower()
    client = TadoClient()
    
    if command == "sync":
        quick = "--quick" in sys.argv
        success = client.sync(quick=quick)
        sys.exit(0 if success else 1)
    
    elif command == "auth":
        success = client.device_auth()
        if success:
            # Do initial sync after auth
            client.sync()
        sys.exit(0 if success else 1)
    
    elif command == "status":
        client.status()
        sys.exit(0)
    
    else:
        print(f"Unknown command: {command}")
        print(__doc__)
        sys.exit(1)


if __name__ == "__main__":
    main()
