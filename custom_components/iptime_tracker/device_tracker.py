"""Platform for sensor integration."""
from homeassistant.util import dt, slugify, Throttle # for update interval
from homeassistant.helpers.event import async_track_point_in_utc_time
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.components.device_tracker import PLATFORM_SCHEMA, DeviceScanner
import homeassistant.helpers.config_validation as cv

from datetime import timedelta
from bs4 import BeautifulSoup
import voluptuous as vol
import asyncio
import logging
import re

from .const import CONF_URL, CONF_ID, CONF_PASSWORD, CONF_TARGET, CONF_NAME, CONF_MAC, CONF_INTERVAL

LOGIN_URN   = '/sess-bin/login_handler.cgi'
LOGOUT_URN  = '/sess-bin/login_session.cgi?logout=1'
WLAN_2G_URN = '/sess-bin/timepro.cgi?tmenu=iframe&smenu=macauth_pcinfo_status&bssidx=0'
WLAN_5G_URN = '/sess-bin/timepro.cgi?tmenu=iframe&smenu=macauth_pcinfo_status&bssidx=65536'

_LOGGER = logging.getLogger(__name__)

API_LIMIT_INTERVAL = timedelta(seconds=5)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Optional(CONF_INTERVAL, default=5): cv.positive_int,
    vol.Required(CONF_URL): cv.string,
    vol.Required(CONF_ID): cv.string,
    vol.Required(CONF_PASSWORD): cv.string,
    vol.Required(CONF_TARGET): vol.All(cv.ensure_list, [{
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_MAC): cv.string,
    }]),
})

async def async_setup_scanner(hass, config_entry, async_see, discovery_info=None):
    """Set up the sensor platform."""
    url = config_entry.get(CONF_URL)
    user_id = config_entry.get(CONF_ID)
    user_pw = config_entry.get(CONF_PASSWORD)
    targets = config_entry.get(CONF_TARGET)

    scan_interval = timedelta(seconds=config_entry.get(CONF_INTERVAL))
    sensors = []

    iAPI = IPTimeAPI(hass, url, user_id, user_pw)
    await iAPI.async_update()

    for target in targets:
        iSensor = IPTimeSensor(target['name'], target['mac'], iAPI)
        await iSensor.async_update()
        sensors += [iSensor]

    async def async_update(now):
        for sensor in sensors:
            await sensor.async_update()
        await asyncio.gather(
            *(
                async_see(mac=f"{sensor.state_attributes['iptime_url']}_{sensor._target_mac}", host_name=sensor.name,
                location_name=sensor.state, attributes=sensor.state_attributes, source_type="ipTIME_Tracker")
                for sensor in sensors
            )
        )

    async def _async_update_interval(now):
        try:
            await async_update(now)
        finally:
            if not hass.is_stopping:
                async_track_point_in_utc_time(
                    hass, _async_update_interval, dt.utcnow() + scan_interval
                )

    await _async_update_interval(None)
    return True


class IPTimeAPI(DeviceScanner):
    """ipTIME API"""

    def __init__(self, hass, url, user_id, user_pw):
        """Initialize the ipTIME API"""
        self._hass = hass
        self._url = url
        self._user_id = user_id
        self._user_pw = user_pw
        self.result = {}

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) \
                Chrome/96.0.4664.45 Safari/537.36",
            "Referer": url
        }
        self.efm_session_id = None
        self.session = async_get_clientsession(self._hass)

    @Throttle(API_LIMIT_INTERVAL)
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
            'init_status': 1,
            'captcha_on': 0,
            'captcha_file': None,
            'username': self._user_id,
            'passwd': self._user_pw,
            'default_passwd': '초기암호:admin(변경필요)',
            'captcha_code': None
        }
        response = None

        try:
            response = await self.session.post(url, headers=self.headers, data=data, timeout=30)
            self.efm_session_id = re.findall(re.compile(r"\w{16}"), await response.text())[0]
        except:
            if not response:
                return False
            elif '<html><script>parent.parent.location = "/sess-bin/login_session.cgi?noauto=1"; //session_timeout </script></html>' in await response.text():
                _LOGGER.error(f"{self._url}: Login Fail !! Please check your login account.")
            else:
                _LOGGER.debug(await response.text())
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
        try:
            await self.session.get(url, headers=self.headers, timeout=30)
        except:
            pass

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
            soup = BeautifulSoup(await response_2g.text(), 'html.parser')
            response_2g_list = soup.find_all('tr')
            response_2g_dict = self.device_parsing(response_2g_list)
            result_dict.update(response_2g_dict)
        except ValueError:
            _LOGGER.debug(f"Session Value Error(2.4g) > {self._url}")
            await self.logout()
        except KeyError:
            _LOGGER.debug(f"Session Key Error(2.4g) > {self._url}")
            await self.logout()
        except Exception:
            _LOGGER.debug(f"Not found [Table](2.4g) > {self._url}")
            await self.logout()

        try:
            soup = BeautifulSoup(await response_5g.text(), 'html.parser')
            response_5g_list = soup.find_all('tr')
            response_5g_dict = self.device_parsing(response_5g_list)
            result_dict.update(response_5g_dict)
        except ValueError:
            _LOGGER.debug(f"Session Value Error(5g) > {self._url}")
            await self.logout()
        except KeyError:
            _LOGGER.debug(f"Session Key Error(5g) > {self._url}")
            await self.logout()
        except Exception:
            _LOGGER.debug(f"Not found [Table](5g) > {self._url}")
            await self.logout()

        result_dict.update({'init': None})
        return result_dict

    def device_parsing(self, response_list):
        result_dict = {}
        if len(response_list) == 0:
            raise Exception('Not found [Table]')

        for device in response_list:
            if len(device.find_all('td')) == 4:
                onclick = device.find_all('td')[0]['onclick']
                gray_text = device.find_all('td')[3]
                ip = re.search(re.compile(r"\d{1,3}.\d{1,3}.\d{1,3}.\d{1,3}"), gray_text.text).group()
                if len(re.findall(re.compile(r"'.+?'"), onclick)) == 3:
                    name = re.findall(re.compile(r"'.+?'"), onclick)[2].replace("'", "")
                else:
                    name = ip
                result_dict[device.find_all('td')[0].text] = {
                    'name': name,
                    'ip': ip,
                    'stay_time': device.find_all('td')[2].text,
                    'state': 'home'
                }
        return result_dict


class IPTimeSensor():
    """Representation of a Sensor."""

    def __init__(self, name, mac, api):
        """Initialize the sensor."""
        self._state = 'N/A'
        self._entity_id = name
        self._target_mac = mac
        self._api = api
        self.result_dict = {}
        self.error_count = 0
        self.error_threshold = 3

    @property
    def device_id(self):
        """Return the device id of the tracker"""
        device_id = f'{slugify(self._api._url)}_{slugify(self._target_mac)}'
        return device_id

    @property
    def name(self):
        """Return the name of the sensor."""
        if self._entity_id:
            self._name = f"iptime_{self._entity_id}"
            return self._name
        else:
            self._name = f"iptime_{self._api._user_id}"
            return self._name

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
            data['stay_time'] = self.result_dict[self._target_mac].get('stay_time')
        else:
            data['stay_time'] = 'N/A'
        data['iptime_url'] = self._api._url

        self._state_attributes = data
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
            self.error_count = 0
            if self._target_mac in self.result_dict:
                self._state = self.result_dict[self._target_mac].get('state')
            else:
                self._state = 'not_home'
        else:
            if self.error_count < self.error_threshold:
                self.error_count += 1
            else:
                self._state = 'N/A'
