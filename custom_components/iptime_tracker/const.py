DOMAIN = 'iptime_tracker'

CONF_URL = 'iptime_url'
CONF_ID = 'iptime_id'
CONF_PASSWORD = 'iptime_pw'
CONF_TARGET = 'targets'
CONF_NAME = 'name'
CONF_MAC = 'mac'
DEFAULT_INTERVAL = 5
RSS_LIMIT = -81

HOSTINFO_URN = '/login/hostinfo2.cgi'
LOGIN_URN = '/sess-bin/login_handler.cgi'
LOGOUT_URN = '/sess-bin/login_session.cgi?logout=1'
WLAN_2G_URN = '/sess-bin/timepro.cgi?tmenu=iframe&smenu=macauth_pcinfo_status&bssidx=0'
WLAN_5G_URN = '/sess-bin/timepro.cgi?tmenu=iframe&smenu=macauth_pcinfo_status&bssidx=65536'
MESH_URN = '/sess-bin/timepro.cgi?tmenu=wirelessconf&smenu=easymesh'
M_LOGIN_URN = '/m_handler.cgi'
M_LOGOUT_URN = '/m_login.cgi?logout=1'
M_WLAN_2G_URN = '/cgi/iux_get.cgi?tmenu=wirelessconf&smenu=macauth&act=status&wlmode=2g&bssidx=0'
M_WLAN_5G_URN = '/cgi/iux_get.cgi?tmenu=wirelessconf&smenu=macauth&act=status&wlmode=5g&bssidx=65536'
M_MESH_URN = '/cgi/iux_get.cgi?tmenu=sysconf&smenu=info&act=status'
MESH_STATION_URN = '/easymesh/api.cgi?key=topology'
BETA_UI_URN = '/ui/'
BETA_SERVICE_URN = '/cgi/service.cgi'
TIME_OUT = 5
