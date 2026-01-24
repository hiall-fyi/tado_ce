#!/usr/bin/env python3
"""
Tado API Client - All-in-one script for Home Assistant
Handles authentication, token refresh, API calls, and rate limit tracking.

Usage:
  python3 tado_api.py sync          # Sync zone data + rate limits
  python3 tado_api.py auth          # Interactive device authorization
  python3 tado_api.py status        # Show current status
  
Config file: /config/.storage/tado_ce/config.json
Output files: /config/.storage/tado_ce/zones.json, etc.
"""

import json
import os
import sys
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError

# Configuration - use persistent storage in .storage directory
# v1.5.2: Moved from custom_components/tado_ce/data/ to .storage/tado_ce/
# This prevents HACS upgrades from overwriting credentials and data files
DATA_DIR = Path("/config/.storage/tado_ce")
LEGACY_DATA_DIR = Path("/config/custom_components/tado_ce/data")

# Auto-migrate from legacy location if needed
if LEGACY_DATA_DIR.exists() and not DATA_DIR.exists():
    import shutil
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    for file in LEGACY_DATA_DIR.glob("*.json"):
        shutil.copy2(file, DATA_DIR / file.name)
    # Copy log file too if exists
    legacy_log = LEGACY_DATA_DIR / "api.log"
    if legacy_log.exists():
        shutil.copy2(legacy_log, DATA_DIR / "api.log")
    print(f"Migrated data from {LEGACY_DATA_DIR} to {DATA_DIR}")

DATA_DIR.mkdir(parents=True, exist_ok=True)

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

# Import API Call Tracker
try:
    from .api_call_tracker import (
        APICallTracker,
        CALL_TYPE_ZONE_STATES,
        CALL_TYPE_WEATHER,
        CALL_TYPE_ZONES,
        CALL_TYPE_MOBILE_DEVICES,
        CALL_TYPE_OVERLAY,
        CALL_TYPE_PRESENCE_LOCK,
        CALL_TYPE_HOME_STATE
    )
    _tracker = None
except ImportError:
    # Fallback for standalone script execution
    _tracker = None
    CALL_TYPE_ZONE_STATES = 1
    CALL_TYPE_WEATHER = 2
    CALL_TYPE_ZONES = 3
    CALL_TYPE_MOBILE_DEVICES = 4
    CALL_TYPE_OVERLAY = 5
    CALL_TYPE_PRESENCE_LOCK = 6
    CALL_TYPE_HOME_STATE = 7

# Import Error Handler
try:
    from .error_handler import get_error_handler, RateLimitError, AuthenticationError
    _error_handler = None
except ImportError:
    # Fallback for standalone script execution
    _error_handler = None
    RateLimitError = Exception
    AuthenticationError = Exception


class TadoAPIError(Exception):
    """Custom exception for Tado API errors."""
    pass


class TadoClient:
    """Tado API client with automatic token management."""
    
    def __init__(self):
        self.config = self._load_config()
        self.access_token = None
        self.rate_limit = {}
        
        # CRITICAL: Use centralized AuthManager to prevent token rotation race conditions
        self._auth_manager = None
        self._api_endpoint_me = None
        self._api_endpoint_homes = None
        try:
            # Try relative import first (when used as module)
            try:
                from .auth_manager import get_auth_manager
                from .const import TADO_AUTH_URL, CLIENT_ID as CONST_CLIENT_ID, API_ENDPOINT_ME, API_ENDPOINT_HOMES
                self._api_endpoint_me = API_ENDPOINT_ME
                self._api_endpoint_homes = API_ENDPOINT_HOMES
            except ImportError:
                # Fall back to absolute import (when used as standalone script)
                import sys
                import os
                sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
                from auth_manager import get_auth_manager
                from const import TADO_AUTH_URL, CLIENT_ID as CONST_CLIENT_ID, API_ENDPOINT_ME, API_ENDPOINT_HOMES
                self._api_endpoint_me = API_ENDPOINT_ME
                self._api_endpoint_homes = API_ENDPOINT_HOMES
            
            self._auth_manager = get_auth_manager(CONFIG_FILE, CONST_CLIENT_ID, TADO_AUTH_URL)
            log.info("Using centralized AuthManager for token management")
        except Exception as e:
            log.warning(f"AuthManager not available ({e}), using standalone token management")
        
        # Initialize API call tracker
        global _tracker
        if _tracker is None:
            try:
                # Try relative import first (when used as module)
                try:
                    from .api_call_tracker import APICallTracker
                except ImportError:
                    # Fall back to absolute import (when used as standalone script)
                    from api_call_tracker import APICallTracker
                
                # Try to get retention days from config (only when used as module)
                retention_days = 14  # default
                try:
                    from .config_manager import ConfigurationManager
                    config_manager = ConfigurationManager(None)
                    retention_days = config_manager.get_api_history_retention_days()
                except:
                    # Use default retention when running as standalone script
                    pass
                
                _tracker = APICallTracker(DATA_DIR, retention_days=retention_days)
                log.info(f"API call tracker initialized with {retention_days} days retention")
            except Exception as e:
                log.warning(f"Failed to initialize API call tracker: {e}")
                _tracker = None
        
        self.tracker = _tracker
        
        # Initialize error handler
        global _error_handler
        if _error_handler is None:
            try:
                # Try relative import first (when used as module)
                try:
                    from .error_handler import get_error_handler
                except ImportError:
                    # Fall back to absolute import (when used as standalone script)
                    from error_handler import get_error_handler
                
                _error_handler = get_error_handler()
                log.info("Error handler initialized")
            except Exception as e:
                log.warning(f"Failed to initialize error handler: {e}")
                _error_handler = None
        
        self.error_handler = _error_handler
    
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
        
        # Calculate reset time using multiple strategies
        calculated_reset_seconds = None
        
        # Strategy 1: Use API-provided reset_seconds if available and valid
        if reset_seconds and reset_seconds > 0:
            calculated_reset_seconds = reset_seconds
        
        # Strategy 2: Calculate from last known reset time (rolling 24h window)
        elif last_reset_utc:
            try:
                last_reset = datetime.fromisoformat(last_reset_utc.replace('Z', '+00:00'))
                next_reset = last_reset + timedelta(hours=24)
                seconds_until_reset = int((next_reset - now_utc).total_seconds())
                
                # If negative, the reset should have happened already
                # This means we haven't detected the new reset yet
                if seconds_until_reset > 0:
                    calculated_reset_seconds = seconds_until_reset
                else:
                    # Reset should have happened, but we haven't detected it
                    # Show "unknown" until we detect the actual reset
                    log.debug(f"Expected reset time passed ({abs(seconds_until_reset)}s ago), waiting for detection")
            except Exception as e:
                log.debug(f"Failed to calculate reset from last_reset_utc: {e}")
        
        # Strategy 3: Use API call history to estimate reset time
        if calculated_reset_seconds is None and self.tracker:
            try:
                # Get oldest call from today
                calls_today = self.tracker.get_call_history(days=1)
                if calls_today:
                    # Sort by timestamp (oldest first)
                    calls_today.sort(key=lambda x: x["timestamp"])
                    oldest_call_time = datetime.fromisoformat(calls_today[0]["timestamp"])
                    
                    # Assume 24h window from first call
                    estimated_reset = oldest_call_time + timedelta(hours=24)
                    seconds_until_reset = int((estimated_reset - now).total_seconds())
                    
                    if seconds_until_reset > 0:
                        calculated_reset_seconds = seconds_until_reset
                        log.debug(f"Estimated reset time from API call history: {seconds_until_reset}s")
            except Exception as e:
                log.debug(f"Failed to estimate reset from API call history: {e}")
        
        # Format reset time for display
        if calculated_reset_seconds and calculated_reset_seconds > 0:
            hours = calculated_reset_seconds // 3600
            mins = (calculated_reset_seconds % 3600) // 60
            reset_human = f"{hours}h {mins}m" if hours > 0 else f"{mins}m"
            reset_at = (now + timedelta(seconds=calculated_reset_seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")
            reset_seconds = calculated_reset_seconds
        else:
            reset_seconds = None
        
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
            "reset_seconds": reset_seconds,
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
        """Make HTTP request and return (response_data, response_headers, status_code)."""
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
                status_code = response.status
                
                if parse_ratelimit:
                    self._parse_ratelimit_headers(resp_headers)
                
                return resp_data, resp_headers, status_code
                
        except HTTPError as e:
            resp_headers = {k.lower(): v for k, v in e.headers.items()}
            if parse_ratelimit:
                self._parse_ratelimit_headers(resp_headers)
            
            try:
                error_body = json.loads(e.read().decode('utf-8'))
            except:
                error_body = {"error": str(e)}
            
            # Raise specific error types for error handler
            if e.code == 429:
                raise RateLimitError(f"Rate limit exceeded: {error_body}")
            elif e.code in (401, 403):
                raise AuthenticationError(f"Authentication failed: {error_body}")
            else:
                raise TadoAPIError(f"HTTP {e.code}: {error_body}")
        
        except URLError as e:
            raise TadoAPIError(f"Network error: {e.reason}")
    
    def refresh_access_token(self) -> bool:
        """Refresh access token using refresh token.
        
        CRITICAL: Use AuthManager if available to prevent race conditions.
        """
        # Try AuthManager first (prevents race conditions)
        if self._auth_manager:
            try:
                token = self._auth_manager.get_access_token()
                if token:
                    self.access_token = token
                    log.info("Access token obtained from AuthManager")
                    return True
                else:
                    log.error("AuthManager failed to get access token")
                    return False
            except Exception as e:
                log.error(f"AuthManager error: {e}, falling back to standalone mode")
                # Fall through to standalone mode
        
        # Standalone mode (fallback)
        refresh_token = self.config.get("refresh_token")
        if not refresh_token:
            log.error("No refresh token available. Run 'auth' first.")
            return False
        
        log.info("Refreshing access token (standalone mode)...")
        
        try:
            data, _, _ = self._http_request(
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
            data, _, _ = self._http_request(
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
                    token_data, _, _ = self._http_request(
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
            # Use centralized API endpoint if available
            me_url = self._api_endpoint_me or "https://my.tado.com/api/v2/me"
            data, _, _ = self._http_request(
                me_url,
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

    def api_call(self, endpoint: str, call_type: int = None) -> dict:
        """Make authenticated API call with tracking and error handling.
        
        Args:
            endpoint: API endpoint (e.g., "zoneStates", "weather")
            call_type: Optional call type code for tracking (auto-detected if not provided)
        
        Returns:
            API response data, or None if all retries failed
        
        Raises:
            RateLimitError: If rate limit is exceeded
            AuthenticationError: If authentication fails
        """
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
        
        # Use centralized API endpoint if available
        homes_base = self._api_endpoint_homes or "https://my.tado.com/api/v2/homes"
        url = f"{homes_base}/{home_id}/{endpoint}"
        
        # Auto-detect call type from endpoint if not provided
        if call_type is None:
            if "zoneStates" in endpoint:
                call_type = CALL_TYPE_ZONE_STATES
            elif "weather" in endpoint:
                call_type = CALL_TYPE_WEATHER
            elif "zones" in endpoint:
                call_type = CALL_TYPE_ZONES
            elif "mobileDevices" in endpoint:
                call_type = CALL_TYPE_MOBILE_DEVICES
            elif "overlay" in endpoint:
                call_type = CALL_TYPE_OVERLAY
            elif "presenceLock" in endpoint:
                call_type = CALL_TYPE_PRESENCE_LOCK
            elif "state" in endpoint:
                call_type = CALL_TYPE_HOME_STATE
        
        # Define the API call function for error handler
        def _make_api_call():
            try:
                data, _, status_code = self._http_request(
                    url,
                    headers={"Authorization": f"Bearer {self.access_token}"}
                )
                
                # Track successful call
                if self.tracker and call_type:
                    self.tracker.record_call(call_type, status_code)
                
                return data
                
            except TadoAPIError as e:
                # Track failed call
                if self.tracker and call_type:
                    # Extract status code from error message
                    status_code = 500  # default
                    if "401" in str(e) or "403" in str(e):
                        status_code = 401
                    elif "429" in str(e):
                        status_code = 429
                    self.tracker.record_call(call_type, status_code)
                
                # Re-raise for error handler to process
                raise
        
        # Use error handler if available, otherwise call directly
        if self.error_handler:
            try:
                result = self.error_handler.handle_api_call(
                    _make_api_call,
                    operation_name=f"API call to {endpoint}"
                )
                return result
            except (RateLimitError, AuthenticationError):
                # Re-raise these specific errors for special handling
                raise
            except Exception as e:
                # Convert other exceptions to TadoAPIError
                raise TadoAPIError(str(e)) from e
        else:
            # Fallback: call directly without error handler
            return _make_api_call()
    
    def sync(self, quick: bool = False) -> bool:
        """Sync zone data and rate limits.
        
        Args:
            quick: If True, only sync zoneStates and weather (2 calls).
                   If False, sync all data including zones and mobileDevices (4 calls).
        """
        sync_type = "quick" if quick else "full"
        log.info(f"Starting {sync_type} sync...")
        
        # Check if weather is enabled
        weather_enabled = self.config.get('weather_enabled', True)
        
        # Check if mobile devices tracking is enabled
        mobile_devices_enabled = self.config.get('mobile_devices_enabled', True)
        
        # Check if frequent mobile device sync is enabled
        mobile_devices_frequent_sync = self.config.get('mobile_devices_frequent_sync', False)
        
        # Check if offset sync is enabled
        offset_enabled = self.config.get('offset_enabled', False)
        
        try:
            # Always fetch zone states (most important)
            zones_data = self.api_call("zoneStates")
            with open(ZONES_FILE, 'w') as f:
                json.dump(zones_data, f, indent=2)
            log.info(f"Zone states saved ({len(zones_data.get('zoneStates', {}))} zones)")
            
            # Fetch weather only if enabled
            if weather_enabled:
                weather_data = self.api_call("weather")
                with open(WEATHER_FILE, 'w') as f:
                    json.dump(weather_data, f, indent=2)
                log.info("Weather data saved")
            else:
                log.info("Weather sync skipped (disabled in config)")
            
            # Always fetch home state (needed for away mode switch)
            home_state = self.api_call("state")
            home_state_file = os.path.join(os.path.dirname(MOBILE_DEVICES_FILE), 'home_state.json')
            with open(home_state_file, 'w') as f:
                json.dump(home_state, f, indent=2)
            log.info(f"Home state saved (presence: {home_state.get('presence')})")
            
            # Fetch mobile devices during quick sync if frequent sync is enabled
            if quick and mobile_devices_enabled and mobile_devices_frequent_sync:
                mobile_data = self.api_call("mobileDevices")
                with open(MOBILE_DEVICES_FILE, 'w') as f:
                    json.dump(mobile_data, f, indent=2)
                log.info(f"Mobile devices saved (frequent sync, {len(mobile_data)} devices)")
            
            if not quick:
                # Full sync: also fetch zone info and mobile devices
                zones_info = self.api_call("zones")
                with open(ZONES_INFO_FILE, 'w') as f:
                    json.dump(zones_info, f, indent=2)
                log.info(f"Zone info saved ({len(zones_info)} zones)")
                
                # Fetch mobile devices only if enabled
                if mobile_devices_enabled:
                    mobile_data = self.api_call("mobileDevices")
                    with open(MOBILE_DEVICES_FILE, 'w') as f:
                        json.dump(mobile_data, f, indent=2)
                    log.info(f"Mobile devices saved ({len(mobile_data)} devices)")
                else:
                    log.info("Mobile devices sync skipped (disabled in config)")
                
                # Fetch temperature offsets if enabled
                if offset_enabled:
                    self._sync_offsets(zones_info)
                
                # Fetch AC zone capabilities (for DRY/FAN modes, fan levels, swing)
                self._sync_ac_capabilities(zones_info)

            
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
    
    def _sync_offsets(self, zones_info: list):
        """Sync temperature offsets for all devices.
        
        Fetches offset for each device and saves to offsets.json keyed by zone_id.
        """
        offsets_file = DATA_DIR / "offsets.json"
        offsets = {}
        
        for zone in zones_info:
            zone_id = str(zone.get('id'))
            zone_type = zone.get('type')
            
            # Only fetch offsets for heating zones (not hot water)
            if zone_type not in ('HEATING', 'AIR_CONDITIONING'):
                continue
            
            for device in zone.get('devices', []):
                serial = device.get('shortSerialNo')
                if serial:
                    try:
                        # Fetch offset for this device
                        offset_data = self._fetch_device_offset(serial)
                        if offset_data is not None:
                            offsets[zone_id] = offset_data
                            log.info(f"Offset for zone {zone_id} (device {serial}): {offset_data}°C")
                        break  # Only need first device per zone
                    except Exception as e:
                        log.warning(f"Failed to fetch offset for device {serial}: {e}")
        
        # Save offsets to file
        try:
            with open(offsets_file, 'w') as f:
                json.dump(offsets, f, indent=2)
            log.info(f"Offsets saved ({len(offsets)} zones)")
        except Exception as e:
            log.error(f"Failed to save offsets: {e}")
    
    def _sync_ac_capabilities(self, zones_info: list):
        """Sync AC zone capabilities.
        
        Fetches capabilities for each AIR_CONDITIONING zone and saves to ac_capabilities.json.
        This includes supported modes (COOL, HEAT, DRY, FAN, AUTO), fan levels, and swing options.
        """
        ac_caps_file = DATA_DIR / "ac_capabilities.json"
        ac_capabilities = {}
        
        for zone in zones_info:
            zone_id = str(zone.get('id'))
            zone_type = zone.get('type')
            
            # Only fetch capabilities for AC zones
            if zone_type != 'AIR_CONDITIONING':
                continue
            
            try:
                # Fetch capabilities for this zone
                caps = self.api_call(f"zones/{zone_id}/capabilities")
                if caps:
                    ac_capabilities[zone_id] = caps
                    # Log what modes are supported
                    modes = [m for m in ['COOL', 'HEAT', 'DRY', 'FAN', 'AUTO'] if m in caps]
                    log.info(f"AC capabilities for zone {zone_id}: modes={modes}")
            except Exception as e:
                log.warning(f"Failed to fetch AC capabilities for zone {zone_id}: {e}")
        
        # Save capabilities to file
        if ac_capabilities:
            try:
                with open(ac_caps_file, 'w') as f:
                    json.dump(ac_capabilities, f, indent=2)
                log.info(f"AC capabilities saved ({len(ac_capabilities)} zones)")
            except Exception as e:
                log.error(f"Failed to save AC capabilities: {e}")
    
    def _fetch_device_offset(self, serial: str) -> float | None:
        """Fetch temperature offset for a specific device."""
        if not self.access_token:
            if not self.refresh_access_token():
                return None
        
        # Use centralized API endpoint if available
        devices_base = "https://my.tado.com/api/v2/devices"
        try:
            from .const import API_ENDPOINT_DEVICES
            devices_base = API_ENDPOINT_DEVICES
        except ImportError:
            pass
        
        url = f"{devices_base}/{serial}/temperatureOffset"
        
        try:
            data, _, _ = self._http_request(
                url,
                headers={"Authorization": f"Bearer {self.access_token}"}
            )
            return data.get("celsius")
        except TadoAPIError as e:
            log.warning(f"Failed to fetch offset for {serial}: {e}")
            return None
    
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
