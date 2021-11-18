"""Platform for sensor integration."""
from homeassistant.util import Throttle # for update interval
from homeassistant.helpers.entity import Entity
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.helpers.aiohttp_client import async_get_clientsession
import homeassistant.helpers.config_validation as cv

from datetime import timedelta
import voluptuous as vol
import logging
import json
import re

from .const import CONF_PASSWORD, CONF_URL, CONF_ID, CONF_PASSWORD, CONF_TARGET, CONF_NAME, CONF_MAC

LOGIN_URN   = "/m_handler.cgi"
LOGOUT_URN  = "/m_login.cgi?logout=1"
HOSTINFO_URN = "/login/hostinfo2.cgi"
WLAN_2G_URN = "/cgi/iux_get.cgi?tmenu=wirelessconf&smenu=macauth&act=status&wlmode=2g&bssidx=0"
WLAN_5G_URN = "/cgi/iux_get.cgi?tmenu=wirelessconf&smenu=macauth&act=status&wlmode=5g&bssidx=65536"

_LOGGER = logging.getLogger(__name__)

API_INTERVAL = timedelta(seconds=5)
SCAN_INTERVAL = timedelta(seconds=3)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_URL): cv.string,
    vol.Required(CONF_ID): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
    vol.Required(CONF_TARGET): vol.All(cv.ensure_list, [{
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_MAC): cv.string,
    }]),
})

async def async_setup_platform(hass, config_entry, async_add_entities, discovery_info=None):
    """Set up the sensor platform."""
    url = config_entry.get(CONF_URL)
    user_id = config_entry.get(CONF_ID)
    user_pw = config_entry.get(CONF_PASSWORD)
    targets = config_entry.get(CONF_TARGET)

    sensors = []

    iAPI = IPTimeAPI(hass, url, user_id, user_pw)
    await iAPI.async_update()

    for target in targets:
        iSensor = IPTimeSensor(target['name'], target['mac'], iAPI)
        await iSensor.async_update()
        sensors += [iSensor]

    async_add_entities(sensors, True)


class IPTimeAPI:
    """ipTIME API"""

    def __init__(self, hass, url, user_id, user_pw):
        """Initialize the ipTIME API"""
        self._hass = hass
        self._url = url
        self._user_id = user_id
        self._user_pw = user_pw
        self._ismobile = True
        self.result = {}

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Linux; Android 5.0; SM-G900P Build/LRX21T) AppleWebKit/537.36 \
                (KHTML, like Gecko) Chrome/80.0.3987.132 Mobile Safari/537.36",
            "Referer": url
        }
        self.efm_session_id = None
        self.session = async_get_clientsession(self._hass)

    @Throttle(API_INTERVAL)
    async def async_update(self):
        """Update function for updating api information."""
        if self.efm_session_id:
            self.result = await self.wlan_check()
        else:
            if await self.login():
                self.result = await self.wlan_check()
            else:
                return False

    async def login(self):
        """Login Function"""
        url = self._url + LOGIN_URN
        data = {
            "captcha_file": "undefined",
            "captcha_code": None,
            "captcha_on": 0,
            "username": self._user_id,
            "passwd": self._user_pw,
        }
        response = None

        try:
            response = await self.session.post(url, headers=self.headers, data=data, timeout=30)
            self.efm_session_id = re.findall(re.compile(r"\w{16}"), await response.text())[0]
        except:
            if response and self._ismobile:
                if '<html><script> top.location = "/";</script></html>' in await response.text():
                    await self.verify_mobile()
                elif '<html><script> if(parent && parent.parent) parent.parent.location = "/";</script></html>' in await response.text():
                    await self.verify_mobile()
                elif '<html><script>parent.parent.location = "/m_login.cgi?noauto=1"; //session_timeout </script></html>' in await response.text():
                    _LOGGER.error(f"{self._url}: Login Fail !! Please check your login account.")
                else:
                    _LOGGER.debug(await response.text())
                    self._ismobile = False
            else:
                _LOGGER.error(f"{self._url}: Login Error !!")
            return False

        if self.efm_session_id:
            _LOGGER.debug(f"{self._url}: Login Success !! [{self.efm_session_id}]")
            return True
        else:
            _LOGGER.error(f"{self._url}: Login Fail !!")
            return False

    async def logout(self):
        """Logout Function"""
        self.efm_session_id = None
        url = self._url + LOGOUT_URN
        await self.session.get(url, headers=self.headers, timeout=30)

    async def verify_mobile(self):
        url = self._url + HOSTINFO_URN
        response = await self.session.get(url, headers=self.headers, timeout=30)
        if "iux" not in await response.text():
            _LOGGER.info(f"{self._url}: This firmware is not supported the mobile app.")
            return False
        if "iux_package_installed" not in await response.text():
            _LOGGER.debug(f"{self._url}: This firmware already contains the mobile package.")
            _LOGGER.error(f"{self._url}: Login Fail !! Please check your login account.")
            return False

        try:
            iux = int(re.findall(re.compile(r"iux=\d"), await response.text())[0].split('=')[1])
            iux_package_installed = int(re.findall(re.compile(r"iux_package_installed=\d"), await response.text())[0].split('=')[1])
            if iux:
                if iux_package_installed:
                    _LOGGER.debug(f"{self._url}: The mobile package is already installed.")
                    _LOGGER.error(f"{self._url}: Login Fail !! Please check your login account.")
                    return False
                else:
                    _LOGGER.error(f"{self._url}: Please install the mobile package.")
                    return False
            else:
                _LOGGER.error(f"{self._url}: This page is not supported the mobile app.")
                return False
        except:
            _LOGGER.error(f"{self._url}: Verify_mobile Function Error")
            return False


    async def wlan_check(self):
        """Wlan Check Function"""
        result_dict = {}

        url_2g = self._url + WLAN_2G_URN
        url_5g = self._url + WLAN_5G_URN
        cookies = {
			'efm_session_id': self.efm_session_id
		}
        try:
            response_2g = await self.session.get(url_2g, headers=self.headers, cookies=cookies, timeout=10)
            response_5g = await self.session.get(url_5g, headers=self.headers, cookies=cookies, timeout=10)
        except:
            _LOGGER.debug(f"WLAN Connect Error > {self._url}")
            await self.logout()
            return result_dict

        try:
            response_2g_json = json.loads(await response_2g.text())
            response_2g_dict = self.json_parsing(response_2g_json)
            result_dict.update(response_2g_dict)
        except ValueError:
            _LOGGER.debug(f"Session Value Error(2.4g) > {self._url}")
            await self.logout()
        except KeyError:
            _LOGGER.debug(f"Session Key Error(2.4g) > {self._url}")
            await self.logout()
        except Exception:
            _LOGGER.debug(f"Not found [stalist](2.4g) > {self._url}")
            await self.logout()

        try:
            response_5g_json = json.loads(await response_5g.text())
            response_5g_dict = self.json_parsing(response_5g_json)
            result_dict.update(response_5g_dict)
        except ValueError:
            _LOGGER.debug(f"Session Value Error(5g) > {self._url}")
            await self.logout()
        except KeyError:
            _LOGGER.debug(f"Session Key Error(5g) > {self._url}")
            await self.logout()
        except Exception:
            _LOGGER.debug(f"Not found [stalist](5g) > {self._url}")
            await self.logout()

        return result_dict


    def json_parsing(self, response_json):
        result_dict = {}
        if 'stalist' not in response_json:
            raise Exception('Not found [stalist]')

        for device in response_json['stalist']:
            if 'mac' in device:
                time = f"{device['day']}일 {device['hour']}:{device['min']}:{device['sec']}"
                state = "home"
                result_dict[device['mac']] = {
                        'name': device['pcname'],
                        'time': time,
                        'state': state
                }
            else:
                continue
        return result_dict


class IPTimeSensor(Entity):
    """Representation of a Sensor."""

    def __init__(self, name, mac, api):
        """Initialize the sensor."""
        self._state = 'N/A'
        self._entity_id = name
        self._target_mac = mac
        self._api = api
        self.result_dict = {}

    @property
    def name(self):
        """Return the name of the sensor."""
        if self._entity_id:
            return f"iptime_tracker_{self._entity_id}"
        else:
            return f"iptime_tracker_{self._api._user_id}"

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def state_attributes(self):
        """Return the optional state attributes."""
        data={}
        data['name'] = self._entity_id
        data['mac_address'] = self._target_mac
        if self._target_mac in self.result_dict:
            data['stay_time'] = self.result_dict[self._target_mac].get('time')
        else:
            data['stay_time'] = 'N/A'
        data['iptime_url'] = self._api._url

        return data

    async def async_update(self):
        """Fetch new state data for the sensor.
        This is the only method that should fetch new data for Home Assistant.
        """
        if self._api is None:
            return
        await self._api.async_update()
        self.result_dict = self._api.result
        if self.result_dict:
            if self._target_mac in self.result_dict:
                self._state = self.result_dict[self._target_mac].get('state')
            else:
                self._state = 'not_home'
        else:
            self._state = 'N/A'
