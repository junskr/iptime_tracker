"""Platform for sensor integration."""
from homeassistant.util import dt, slugify, Throttle  # for update interval
from homeassistant.helpers.event import async_track_point_in_utc_time
#from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.components.device_tracker import PLATFORM_SCHEMA, DeviceScanner
import homeassistant.helpers.config_validation as cv
from homeassistant.components.device_tracker.const import CONF_SCAN_INTERVAL

from datetime import timedelta
from bs4 import BeautifulSoup
from json import loads
import voluptuous as vol
import requests
import asyncio
import logging
import re

from .const import (
    CONF_URL,
    CONF_ID,
    CONF_PASSWORD,
    CONF_TARGET,
    CONF_NAME,
    CONF_MAC,
    DEFAULT_INTERVAL,
    HOSTINFO_URN,
    LOGIN_URN,
    LOGOUT_URN,
    WLAN_2G_URN,
    WLAN_5G_URN,
    MESH_URN,
    M_LOGIN_URN,
    M_LOGOUT_URN,
    M_WLAN_2G_URN,
    M_WLAN_5G_URN,
    M_MESH_URN,
    MESH_STATION_URN,
    TIME_OUT,
    BETA_UI_URN,
    BETA_SERVICE_URN,
    RSS_LIMIT
)

_LOGGER = logging.getLogger(__name__)
API_LIMIT_INTERVAL = timedelta(seconds=4)

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_URL): cv.string,
        vol.Required(CONF_ID): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_TARGET): vol.All(
            cv.ensure_list,
            [
                {
                    vol.Required(CONF_NAME): cv.string,
                    vol.Required(CONF_MAC): cv.string,
                }
            ],
        ),
    }
)


async def async_setup_scanner(hass, config_entry, async_see, discovery_info=None):
    """Set up the sensor platform."""
    url = config_entry.get(CONF_URL)
    user_id = config_entry.get(CONF_ID)
    user_pw = config_entry.get(CONF_PASSWORD)
    targets = config_entry.get(CONF_TARGET)

    scan_interval = config_entry.get(
        CONF_SCAN_INTERVAL, timedelta(seconds=DEFAULT_INTERVAL)
    )
    sensors = []

    iAPI = IPTimeAPI(hass, url, user_id, user_pw)
    await iAPI.async_update()

    for target in targets:
        iSensor = IPTimeSensor(target["name"], target["mac"], iAPI)
        await iSensor.async_update()
        sensors += [iSensor]

    async def async_update(now):
        for sensor in sensors:
            await sensor.async_update()
        await asyncio.gather(
            *(
                async_see(
                    mac=f"{sensor.state_attributes['iptime_url']}_{sensor._target_mac}",
                    host_name=sensor.name,
                    location_name=sensor.state,
                    attributes=sensor.state_attributes,
                    source_type="ipTIME_Tracker",
                )
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
        self._user_id = user_id
        self._user_pw = user_pw
        self._ismobile = False
        self._ismesh = False
        # 2024.05.07. Beta UI 지원
        self._beta_ui = False

        self.result = {}
        if not "http" in url:
            self._url = "http://" + url
        else:
            self._url = url

        header_base = {
            "User-Agent": "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36",
            "Referer": self._url,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Encoding": "gzip",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7,zh-CN;q=0.6,zh-TW;q=0.5,zh;q=0.4",
            "Upgrade-Insecure-Requests": "1",
            "Content-type": "text/plain; charset=utf-8"
        }
        self.headers = header_base.copy()
        self.json_headers = header_base.copy()
        self.headers['Content-type'] = "text/plain; charset=utf-8"
        self.json_headers['Content-type'] = "application/json; charset=utf-8"

        self.efm_session_id = None
        #self.session = async_get_clientsession(self._hass)
        self.loop = asyncio.get_event_loop()

    @Throttle(API_LIMIT_INTERVAL)
    async def async_update(self):
        """Update function for updating api information."""
        # Step 4. (반복)로그인되어 있을 경우, 재실체크 수행
        if self.efm_session_id:
            if self._beta_ui:
                self.result = await self.beta_ui_wlan_check()
                await self.session_update_beta_ui()
            elif self._ismobile:
                self.result = await self.m_wlan_check()
            else:
                self.result = await self.wlan_check()

        else:
            # Step 1. (최초 1회)Beta UI 지원 여부 확인
            if await self.verify_beta_ui():
                # Beta UI를 지원하면
                _LOGGER.info(f"[ipTIME-BetaUI] {self._url}")
                self._beta_ui = True
                if await self.login_beta_ui():
                    await self.beta_ui_check_mesh()
                    return True
                else:
                    return False

            # Step 2. (최초 1회)모바일 페이지 지원 여부 확인
            if not await self.verify_mobile():
                return False

            # Step 3. (최초 1회)모바일 페이지 지원 - 모바일 로그인, MESH 체크, 재실체크
            if self._ismobile:
                _LOGGER.info(f"[ipTIME-Mobile] {self._url}")
                if await self.m_login():
                    await self.m_check_mesh()
                    self.result = await self.m_wlan_check()
                    return True
                else:
                    return False

            # Step 3. (최초 1회)모바일 페이지 미지원 - PC 로그인, MESH 체크, 재실체크
            else:
                _LOGGER.info(f"[ipTIME-PC] {self._url}")
                if await self.login():
                    await self.check_mesh()
                    self.result = await self.wlan_check()
                    return True
                else:
                    return False

    async def verify_beta_ui(self):
        """
        # 2024.05.07. Beta UI 지원 (/ui/)
        """
        url = self._url + BETA_UI_URN
        response = await self.loop.run_in_executor(None, lambda: requests.get(url, headers=self.headers, timeout=TIME_OUT))
        try:
            response = await self.loop.run_in_executor(None, lambda: requests.get(url, headers=self.headers, timeout=TIME_OUT))
            if "/cgi/service.cgi" in response.text:
                return True
        except:
            return False

    async def login_beta_ui(self):
        """
        # 2024.05.07. Beta UI 지원 (/)
        """
        url = self._url + BETA_SERVICE_URN
        data = {
            "method":"session/login",
            "params":{
                "id": self._user_id,
                "pw": self._user_pw
            }
        }
        response = await self.loop.run_in_executor(None, lambda: requests.post(url, headers=self.json_headers, json=data, timeout=TIME_OUT))
        response_json = response.json()
        if response_json['result']:
            self.efm_session_id = response.cookies['efm_session_id']
            _LOGGER.debug(f"{self._url}: (B)Login Success !! [{self.efm_session_id}]")
            return True
        else:
            if response_json['error']:
                if response_json['error']['code'] == -31996:
                    _LOGGER.error(f"{self._url}: (B)Login Fail !!")
                    return False
                elif response_json['error']['code'] == -31997:
                    _LOGGER.error(f"{self._url}: (B)Login Fail !! Check Captcha settings.")
                    return False
                else:
                    _LOGGER.error(f"{self._url}: (B)Login Fail !!")
                    _LOGGER.debug(response_json['error'])
                    return False
        return False

    async def session_update_beta_ui(self):
        """
        # 2024.05.07. Beta UI 지원 (/)
        """
        url = self._url + BETA_SERVICE_URN
        cookies = {"efm_session_id": self.efm_session_id}
        data = {
            "method":"session/update"
        }
        try:
            response = await self.loop.run_in_executor(None, lambda: requests.post(url, headers=self.json_headers, json=data, cookies=cookies, timeout=TIME_OUT))
        except:
            return False
        #_LOGGER.debug(f"[BetaUI 세션유지 테스트] {response.json()}")

    async def verify_mobile(self):
        """
        # aiohttp 버전이 업데이트 되면서, 비정상적인 HTTP Header를 읽을 수 없게끔 패치되었습니다.
        # 따라서 기존 async_get_clientsession를 이용하지 않고, 임의로 requests를 비동기 처리하도록 수정합니다.
        # 그러므로, await response.text()도 response.text로 모두 변경합니다.
        # loads(response.text)도 response.json()로 변경할 수 있지만, 추후 aiohttp를 다시 이용할 수 있으므로 수정하지 않고 남겨둡니다.

        # Version 1.
        response = await self.session.get(url, headers=self.headers, timeout=TIME_OUT)
        _LOGGER.debug(await response.text())

        # Version 2.
        async with self.session.get(url, headers=self.headers, timeout=TIME_OUT) as response:
            _LOGGER.debug(await response.text())

        # Version 3.
        response = await self.loop.run_in_executor(None, lambda: requests.get(url, headers=self.headers, timeout=TIME_OUT))
        _LOGGER.debug(await response.text())
        """

        url = self._url + HOSTINFO_URN

        try:
            # response = await self.session.get(
            #    url, headers=self.headers, timeout=TIME_OUT
            # )
            response = await self.loop.run_in_executor(None, lambda: requests.get(url, headers=self.headers, timeout=TIME_OUT))
            #_LOGGER.info(f"[verify_mobile response] {response.text}")

            product_name = (
                re.search(
                    re.compile(r"product_name=[ a-zA-Z0-9]+"), response.text
                )
                .group()
                .split("=")[1]
            )
        except:
            _LOGGER.error(f"{self._url}: The page cannot be accessed.")
            return True  # False

        if "iux" not in response.text:
            self._ismobile = False
            _LOGGER.debug(
                f"{self._url}: [{product_name}] This firmware is not supported the mobile app."
            )
            return True
        if "iux_package_installed" not in response.text:
            self._ismobile = True
            # _LOGGER.debug(f"{self._url}: This firmware already contains the mobile package.")
            return True

        try:
            iux = int(
                re.search(re.compile(r"iux=\d"), response.text)
                .group()
                .split("=")[1]
            )
            iux_package_installed = int(
                re.search(
                    re.compile(r"iux_package_installed=\d"), response.text
                )
                .group()
                .split("=")[1]
            )
            if iux:
                if iux_package_installed:
                    self._ismobile = True
                    # _LOGGER.debug(f"{self._url}: The mobile package is already installed.")
                    return True
                else:
                    self._ismobile = False
                    _LOGGER.debug(
                        f"{self._url}: [{product_name}] Please install the mobile package."
                    )
                    return True
            else:
                self._ismobile = False
                _LOGGER.debug(
                    f"{self._url}: [{product_name}] This page is not supported the mobile app."
                )
                return True
        except:
            self._ismobile = False
            _LOGGER.error(
                f"{self._url}: [{product_name}] Verify_mobile Function Error")
            return False

    async def check_mesh(self):
        url = self._url + MESH_URN
        cookies = {"efm_session_id": self.efm_session_id}
        try:
            # response = await self.session.get(
            #    url, headers=self.headers, cookies=cookies, timeout=TIME_OUT
            # )
            response = await self.loop.run_in_executor(None, lambda: requests.get(url, headers=self.headers, cookies=cookies, timeout=TIME_OUT))
            soup = BeautifulSoup(response.text, "html.parser")
            mesh_mode = soup.find("input", attrs={"id": "mode_none"})
            if not mesh_mode:
                self._ismesh = False
                return False
            if "checked" in mesh_mode.attrs:
                self._ismesh = False
                return False
            else:
                self._ismesh = True
                return True
        except:
            return False

    async def m_check_mesh(self):
        url = self._url + M_MESH_URN
        cookies = {"efm_session_id": self.efm_session_id}
        response = await self.loop.run_in_executor(None, lambda: requests.get(url, headers=self.headers, cookies=cookies, timeout=TIME_OUT))
        try:
            response = await self.loop.run_in_executor(None, lambda: requests.get(url, headers=self.headers, cookies=cookies, timeout=TIME_OUT))
            # response = await self.session.get(
            #     url, headers=self.headers, cookies=cookies, timeout=TIME_OUT
            # )
            response_json = loads(response.text)

            if "easymesh" in response_json:
                self._ismesh = True
                return True
            else:
                self._ismesh = False
                return False
        except:
            return False

    async def beta_ui_check_mesh(self):
        """
        # 2024.05.27. Beta UI 지원 (/)
        """
        url = self._url + BETA_SERVICE_URN
        cookies = {"efm_session_id": self.efm_session_id}
        data = {
            "method":"easymesh/info"
        }
        response = await self.loop.run_in_executor(None, lambda: requests.post(url, headers=self.json_headers, cookies=cookies, json=data,timeout=TIME_OUT))

        response_json = response.json()
        if response_json['result']:
            active = response_json['result'].get('active')
            if active:
                self._ismesh = True
                return False
            else:
                self._ismesh = False
                return False
        return False

    async def login(self):
        """Login Function"""
        url = self._url + LOGIN_URN
        data = {
            "username": self._user_id,
            "passwd": self._user_pw,
        }
        response = None

        try:
            response = await self.loop.run_in_executor(None, lambda: requests.post(url, headers=self.headers, data=data, timeout=TIME_OUT))
            #_LOGGER.info(f"[login_response] {response.text}")
            # response = await self.session.post(
            #     url, headers=self.headers, data=data, timeout=TIME_OUT
            # )
            self.efm_session_id = re.findall(
                re.compile(r"\w{16}"), response.text
            )[0]
        except:
            if not response:
                return False
            elif (
                '<html><script>parent.parent.location = "/sess-bin/login_session.cgi?noauto=1"; //session_timeout </script></html>'
                in response.text
            ):
                _LOGGER.error(
                    f"{self._url}: Login Fail !! Please check your login account."
                )
            else:
                _LOGGER.debug(response.text)
            return False

        if self.efm_session_id:
            _LOGGER.debug(
                f"{self._url}: Login Success !! [{self.efm_session_id}]")
            return True
        else:
            _LOGGER.error(f"{self._url}: Login Fail !!")
            return False

    async def m_login(self):
        """Mobile Login Function"""
        url = self._url + M_LOGIN_URN
        data = {
            "username": self._user_id,
            "passwd": self._user_pw,
        }
        response = None

        try:
            response = await self.loop.run_in_executor(None, lambda: requests.post(url, headers=self.headers, data=data, timeout=TIME_OUT))
            # response = await self.session.post(
            #     url, headers=self.headers, data=data, timeout=TIME_OUT
            # )
            self.efm_session_id = re.findall(
                re.compile(r"\w{16}"), response.text
            )[0]
        except:
            if not response:
                return False
            if response and self._ismobile:
                if (
                    '<html><script> top.location = "/";</script></html>'
                    in response.text
                ):
                    await self.verify_mobile()
                    return False
                elif (
                    '<html><script> if(parent && parent.parent) parent.parent.location = "/";</script></html>'
                    in response.text
                ):
                    await self.verify_mobile()
                    return False
                elif (
                    '<html><script>parent.parent.location = "/m_login.cgi?noauto=1"; //session_timeout </script></html>'
                    in response.text
                ):
                    _LOGGER.error(
                        f"{self._url}: Login Fail !! Please check your login account."
                    )
                    return False
                else:
                    _LOGGER.debug(response.text)
            else:
                return False

        if self.efm_session_id:
            _LOGGER.debug(
                f"{self._url}: M_Login Success !! [{self.efm_session_id}]")
            return True
        else:
            _LOGGER.error(f"{self._url}: Login Fail !!")
            return False

    async def logout(self):
        """Logout Function"""
        self.efm_session_id = None
        self._ismobile = False
        self._ismesh = False
        url = self._url + LOGOUT_URN
        try:
            # await self.session.get(url, headers=self.headers, timeout=TIME_OUT)
            await self.loop.run_in_executor(None, lambda: requests.get(url, headers=self.headers, timeout=TIME_OUT))
        except:
            pass

    async def m_logout(self):
        """Mobile Logout Function"""
        self.efm_session_id = None
        self._ismobile = False
        self._ismesh = False
        url = self._url + M_LOGOUT_URN
        try:
            # await self.session.get(url, headers=self.headers, timeout=TIME_OUT)
            await self.loop.run_in_executor(None, lambda: requests.get(url, headers=self.headers, timeout=TIME_OUT))
        except:
            return False

    async def wlan_check(self):
        """Wlan Check Function"""
        result_dict = {}

        url_2g = self._url + WLAN_2G_URN
        url_5g = self._url + WLAN_5G_URN
        cookies = {"efm_session_id": self.efm_session_id}

        try:
            # response_2g = await self.session.get(
            #     url_2g, headers=self.headers, cookies=cookies, timeout=TIME_OUT
            # )
            response_2g = await self.loop.run_in_executor(None, lambda: requests.get(url_2g, headers=self.headers, cookies=cookies, timeout=TIME_OUT))
            soup = BeautifulSoup(response_2g.text, "html.parser")
            response_2g_list = soup.find_all("tr")
            response_2g_dict = self.device_parsing(
                response_2g_list, band="2.4GHz")
            result_dict.update(response_2g_dict)
        except ValueError:
            _LOGGER.debug(f"Session Value Error(2.4g) > {self._url}")
            await self.logout()
            return {"session": False}
        except KeyError:
            # _LOGGER.debug(f"Session Key Error(2.4g) > {self._url}")
            result_dict["session"] = False
        except:
            _LOGGER.debug(f"2.4G WLAN Connect Error > {self._url}")
            await self.logout()
            return result_dict

        try:
            # response_5g = await self.session.get(
            #     url_5g, headers=self.headers, cookies=cookies, timeout=TIME_OUT
            # )
            response_5g = await self.loop.run_in_executor(None, lambda: requests.get(url_5g, headers=self.headers, cookies=cookies, timeout=TIME_OUT))
            soup = BeautifulSoup(response_5g.text, "html.parser")
            response_5g_list = soup.find_all("tr")
            response_5g_dict = self.device_parsing(
                response_5g_list, band="5GHz")
            result_dict.update(response_5g_dict)
        except ValueError:
            _LOGGER.debug(f"Session Value Error(5g) > {self._url}")
            await self.logout()
            return {"session": False}
        except KeyError:
            # _LOGGER.debug(f"Session Key Error(5g) > {self._url}")
            result_dict["session"] = False
        except:
            _LOGGER.debug(f"5G WLAN Connect Error > {self._url}")
            await self.logout()
            return result_dict

        if self._ismesh:
            try:
                response_mesh_dict = await self.get_mesh_station()
                result_dict.update(response_mesh_dict)
            except KeyError:
                # _LOGGER.debug(f"Session Key Error(Mesh) > {self._url}")
                result_dict["session"] = False
            except:
                await self.logout()
                return result_dict

        if not result_dict["session"]:
            await self.logout()
        return result_dict

    async def beta_ui_wlan_check(self):
        """Wlan Check Function for Beta UI
        # 2024.05.07. Beta UI 지원 (/)
        """
        url = self._url + BETA_SERVICE_URN
        cookies = {"efm_session_id": self.efm_session_id}
        data = {
            "method":"network/interface/lan/stations"
        }
        try:
            response = await self.loop.run_in_executor(None, lambda: requests.post(url, headers=self.json_headers, json=data, cookies=cookies, timeout=TIME_OUT))
            response_json = response.json()
        except:
            return {"session": False}

        result_dict = dict()
        if response_json['result']:
            result_dict = self.beta_ui_device_parsing(response_json['result'])
            if self._ismesh:
                try:
                    response_mesh_dict = await self.get_mesh_station()
                    result_dict.update(response_mesh_dict)
                except KeyError:
                    # _LOGGER.debug(f"Session Key Error(Mesh) > {self._url}")
                    result_dict["session"] = False
                except:
                    await self.logout()
                    return result_dict
            result_dict["session"] = True
        else:
            error_dict = response_json.get('error')
            if error_dict:
                if error_dict.get('code') == -31998:
                    # Unauthenticated
                    self.efm_session_id = None
                    result_dict = {"session": False}
                else:
                    result_dict = {"session": False}
            else:
                result_dict = {"session": True}

        #_LOGGER.debug(f"[DevLog-BetaUI-WLAN] {result_dict}")
        return result_dict

    def beta_ui_device_parsing(self, device_list):
        result_dict = dict()
        for device in device_list:
            connect_type = device['connection']['type']
            if connect_type != 'wireless':
                continue

            bss = device['connection'][connect_type]['bss']
            if bss == '5g.1':
                band = "5GHz"
            elif bss == '2g.1':
                band = "2.4GHz"
            else:
                band = bss

            rss = device['connection'][connect_type]['rssi']
            if rss < RSS_LIMIT:
                state = "not_home"
            else:
                state = "home"

            connected_seconds = device['connection'][connect_type]['duration']
            days = timedelta(seconds=connected_seconds).days
            (hours, minutes, seconds) = str(timedelta(seconds=connected_seconds)).split(":")
            connected_time = f"{days}일 {hours}시간 {minutes}분 {seconds}초"

            result_dict[device['mac'].replace(":", "-")] = {
                "ip": device['info']['ip'],
                "band": band,
                "stay_time": connected_time,
                "rssi": rss,
                "down_speed": device['connection'][connect_type]['down_speed'],
                "up_speed": device['connection'][connect_type]['up_speed'],
                "down_bytes": device['connection'][connect_type]['down_bytes'],
                "up_bytes": device['connection'][connect_type]['up_bytes'],
                "state": state
            }
        return result_dict

    async def m_wlan_check(self):
        """Wlan Check Function"""
        result_dict = {}

        url_2g = self._url + M_WLAN_2G_URN
        url_5g = self._url + M_WLAN_5G_URN
        cookies = {"efm_session_id": self.efm_session_id}

        try:
            response_2g = await self.loop.run_in_executor(None, lambda: requests.get(url_2g, headers=self.headers, cookies=cookies, timeout=TIME_OUT))
            # response_2g = await self.session.get(
            #     url_2g, headers=self.headers, cookies=cookies, timeout=TIME_OUT
            # )
            response_2g_json = loads(response_2g.text)
            response_2g_dict = self.json_parsing(
                response_2g_json, band="2.4GHz")
            result_dict.update(response_2g_dict)
        except ValueError:
            _LOGGER.debug(f"Mobile Session Value Error(2.4g) > {self._url}")
            await self.m_logout()
            return {"session": False}
        except KeyError:
            # 유선공유기일 경우 이곳 KeyError 발생
            # _LOGGER.debug(f"Mobile Session Key Error(2.4g) > {self._url}")
            result_dict["session"] = False
        except:
            _LOGGER.debug(f"2.4G WLAN Connect Error > {self._url}")
            await self.m_logout()
            return result_dict

        try:
            response_5g = await self.loop.run_in_executor(None, lambda: requests.get(url_5g, headers=self.headers, cookies=cookies, timeout=TIME_OUT))
            # response_5g = await self.session.get(
            #     url_5g, headers=self.headers, cookies=cookies, timeout=TIME_OUT
            # )
            response_5g_json = loads(response_5g.text)
            response_5g_dict = self.json_parsing(response_5g_json, band="5GHz")
            result_dict.update(response_5g_dict)
        except ValueError:
            _LOGGER.debug(f"Mobile Session Value Error(5g) > {self._url}")
            await self.m_logout()
            return {"session": False}
        except KeyError:
            # 유선공유기일 경우 이곳 KeyError 발생
            # _LOGGER.debug(f"Mobile Session Key Error(5g) > {self._url}")
            result_dict["session"] = False
        except:
            _LOGGER.debug(f"5G WLAN Connect Error > {self._url}")
            await self.m_logout()
            return result_dict

        if self._ismesh:
            try:
                response_mesh_dict = await self.get_mesh_station()
                result_dict.update(response_mesh_dict)
            except KeyError:
                # _LOGGER.debug(f"Mobile Session Key Error(Mesh) > {self._url}")
                result_dict["session"] = False
            except:
                await self.m_logout()
                return result_dict

        if not result_dict["session"]:
            await self.m_logout()
        return result_dict

    async def get_mesh_station(self):
        from datetime import timedelta

        result_dict = {}
        url = self._url + MESH_STATION_URN
        cookies = {"efm_session_id": self.efm_session_id}
        try:
            response = await self.loop.run_in_executor(None, lambda: requests.get(url, headers=self.headers, cookies=cookies, timeout=TIME_OUT))
            # response = await self.session.get(
            #     url, headers=self.headers, cookies=cookies, timeout=TIME_OUT
            # )
            device_list = loads(response.text)["station"]
        except:
            raise KeyError()

        result_dict["session"] = True
        for device in device_list:
            if (
                "connection" in device
                and device["connection"] != "Unknown"
                and device["connection"] != "WIRED"
            ):
                if "mac" in device:
                    connected_seconds = device["timestamp"] - \
                        device["connected_ts"]
                    days = timedelta(seconds=connected_seconds).days
                    (hours, minutes, seconds) = str(
                        timedelta(seconds=connected_seconds)
                    ).split(":")
                    connected_time = f"{days}일 {hours}시간 {minutes}분 {seconds}초"
                    if "ip" in device:
                        ip = device["ip"]
                    else:
                        ip = "N/A"

                    rss = device.get('rssi')
                    if rss and rss < RSS_LIMIT:
                        state = "not_home"
                    else:
                        state = "home"

                    bss = device["mode"]
                    if bss == '5G':
                        band = "5GHz"
                    elif bss == '2.4G':
                        band = "2.4GHz"
                    else:
                        band = bss

                    result_dict[device["mac"].replace(":", "-")] = {
                        "ip": ip,
                        "band": band,
                        "stay_time": connected_time,
                        "rssi": rss,
                        "state": state,
                        "down_speed": device.get('down_speed'),
                        "up_speed": device.get('up_speed'),
                        "down_bytes": device.get('down_bytes'),
                        "up_bytes": device.get('up_bytes'),
                    }

        return result_dict

    def device_parsing(self, response_list, band):
        result_dict = {}
        if len(response_list) == 0:
            raise KeyError()

        for device in response_list:
            if len(device.find_all("td")) == 4:
                gray_text = device.find_all("td")[3]
                if len(gray_text):
                    ip = re.search(
                        re.compile(
                            r"\d{1,3}.\d{1,3}.\d{1,3}.\d{1,3}"), gray_text.text
                    ).group()
                else:
                    ip = "N/A"
                result_dict[device.find_all("td")[0].text] = {
                    "ip": ip,
                    "band": band,
                    "stay_time": device.find_all("td")[2].text,
                    "state": "home",
                }
            else:
                result_dict["session"] = True
        return result_dict

    def json_parsing(self, response_json, band):
        result_dict = {}
        if "stalist" not in response_json:
            raise KeyError()

        for device in response_json["stalist"]:
            if "mac" in device:
                if device["ipaddr"]:
                    ip = device["ipaddr"]
                else:
                    ip = False
                connected_time = f"{device['day']}일 {device['hour']}시간 {device['min']}분 {device['sec']}초"
                result_dict[device["mac"]] = {
                    "ip": ip,
                    "band": band,
                    "stay_time": connected_time,
                    "state": "home",
                }
            else:
                result_dict["session"] = True
        return result_dict


class IPTimeSensor:
    """Representation of a Sensor."""

    def __init__(self, name, mac, api) -> None:
        """Initialize the sensor."""
        self._state = "N/A"
        self._entity_id = name
        self._target_mac = mac.replace(":", "-")
        self._api = api
        self.result_dict = {}
        self.error_count = 0
        self.error_threshold = 3
        self.not_home_count = 0
        self.not_home_threshold = 1

    @property
    def device_id(self):
        """Return the device id of the tracker"""
        device_id = f"{slugify(self._api._url)}_{slugify(self._target_mac)}"
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
        data = {}
        data["name"] = self._entity_id
        data["mac_address"] = self._target_mac
        data["iptime_url"] = self._api._url
        if self.result_dict:
            if self._target_mac in self.result_dict:
                data["stay_time"] = self.result_dict[self._target_mac].get("stay_time", "N/A")
                data["band"] = self.result_dict[self._target_mac].get("band", "N/A")
                data["ip"] = self.result_dict[self._target_mac].get("ip", "N/A")
                # for Beta UI
                data["rssi"] = self.result_dict[self._target_mac].get("rssi", "N/A")
                data["up_speed"] = self.result_dict[self._target_mac].get("up_speed", "N/A")
                data["down_speed"] = self.result_dict[self._target_mac].get("down_speed", "N/A")
                data["up_bytes"] = self.result_dict[self._target_mac].get("up_bytes", "N/A")
                data["down_bytes"] = self.result_dict[self._target_mac].get("down_bytes", "N/A")
            else:
                data["stay_time"] = "N/A"
                data["band"] = "N/A"
                data["ip"] = "N/A"
                # for Beta UI
                data["rssi"] = "N/A"
                data["up_speed"] = "N/A"
                data["down_speed"] = "N/A"
                data["up_bytes"] = "N/A"
                data["down_bytes"] = "N/A"
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
            if not self.result_dict["session"]:
                return

            self.error_count = 0
            if self._target_mac in self.result_dict:
                self.not_home_count = 0
                self._state = self.result_dict[self._target_mac].get("state")
            else:
                if self.not_home_count < self.not_home_threshold:
                    self.not_home_count += 1
                else:
                    self._state = "not_home"
        else:
            if self.error_count < self.error_threshold:
                self.error_count += 1
            else:
                self._state = "N/A"
