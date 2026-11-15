#!/usr/bin/env python3
"""Read the Windows Wi-Fi signal quality / RSSI of the earpick AP.

Uses the native WLAN API, so it is not affected by localized `netsh` output.
Returns (bars, rssi_dbm, quality, ssid); bars is 0..4, unknown values are None.
"""
import ctypes
from ctypes import wintypes

wlanapi = ctypes.WinDLL("wlanapi.dll")


class GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


class WLAN_INTERFACE_INFO(ctypes.Structure):
    _fields_ = [
        ("InterfaceGuid", GUID),
        ("strInterfaceDescription", wintypes.WCHAR * 256),
        ("isState", wintypes.DWORD),
    ]


class WLAN_INTERFACE_INFO_LIST(ctypes.Structure):
    _fields_ = [
        ("dwNumberOfItems", wintypes.DWORD),
        ("dwIndex", wintypes.DWORD),
        ("InterfaceInfo", WLAN_INTERFACE_INFO * 1),
    ]


class DOT11_SSID(ctypes.Structure):
    _fields_ = [
        ("uSSIDLength", wintypes.ULONG),
        ("ucSSID", ctypes.c_ubyte * 32),
    ]


class WLAN_ASSOCIATION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("dot11Ssid", DOT11_SSID),
        ("dot11BssType", wintypes.DWORD),
        ("dot11Bssid", ctypes.c_ubyte * 6),
        ("dot11PhyType", wintypes.DWORD),
        ("uDot11PhyIndex", wintypes.ULONG),
        ("wlanSignalQuality", wintypes.ULONG),
        ("ulRxRate", wintypes.ULONG),
        ("ulTxRate", wintypes.ULONG),
    ]


class WLAN_SECURITY_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("bSecurityEnabled", wintypes.BOOL),
        ("bOneXEnabled", wintypes.BOOL),
        ("dot11AuthAlgorithm", wintypes.DWORD),
        ("dot11CipherAlgorithm", wintypes.DWORD),
    ]


class WLAN_CONNECTION_ATTRIBUTES(ctypes.Structure):
    _fields_ = [
        ("isState", wintypes.DWORD),
        ("wlanConnectionMode", wintypes.DWORD),
        ("strProfileName", wintypes.WCHAR * 256),
        ("wlanAssociationAttributes", WLAN_ASSOCIATION_ATTRIBUTES),
        ("wlanSecurityAttributes", WLAN_SECURITY_ATTRIBUTES),
    ]


wlanapi.WlanOpenHandle.argtypes = [
    wintypes.DWORD, ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD),
    ctypes.POINTER(wintypes.HANDLE),
]
wlanapi.WlanOpenHandle.restype = wintypes.DWORD
wlanapi.WlanEnumInterfaces.argtypes = [
    wintypes.HANDLE, ctypes.c_void_p,
    ctypes.POINTER(ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)),
]
wlanapi.WlanEnumInterfaces.restype = wintypes.DWORD
wlanapi.WlanQueryInterface.argtypes = [
    wintypes.HANDLE, ctypes.POINTER(GUID), ctypes.c_int, ctypes.c_void_p,
    ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_void_p),
    ctypes.POINTER(ctypes.c_int),
]
wlanapi.WlanQueryInterface.restype = wintypes.DWORD
wlanapi.WlanFreeMemory.argtypes = [ctypes.c_void_p]
wlanapi.WlanFreeMemory.restype = None
wlanapi.WlanCloseHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
wlanapi.WlanCloseHandle.restype = wintypes.DWORD


def _quality_to_bars(quality):
    if quality is None:
        return None
    if quality >= 75:
        return 4
    if quality >= 50:
        return 3
    if quality >= 25:
        return 2
    if quality > 0:
        return 1
    return 0


def _rssi_to_bars(rssi):
    if rssi is None:
        return None
    if rssi >= -50:
        return 4
    if rssi >= -60:
        return 3
    if rssi >= -70:
        return 2
    if rssi >= -80:
        return 1
    return 0


def get_signal(timeout_ssid=True):
    """Return (bars, rssi_dbm, quality, ssid) for the connected Wi-Fi AP."""
    handle = wintypes.HANDLE()
    negotiated = wintypes.DWORD()
    if wlanapi.WlanOpenHandle(2, None, ctypes.byref(negotiated), ctypes.byref(handle)) != 0:
        return None, None, None, None

    try:
        info_ptr = ctypes.POINTER(WLAN_INTERFACE_INFO_LIST)()
        if wlanapi.WlanEnumInterfaces(handle, None, ctypes.byref(info_ptr)) != 0:
            return None, None, None, None
        try:
            count = info_ptr.contents.dwNumberOfItems
            items = (WLAN_INTERFACE_INFO * count).from_address(
                ctypes.addressof(info_ptr.contents.InterfaceInfo)
            )
            best = None
            for item in items:
                if item.isState != 1:  # wlan_interface_state_connected
                    continue
                data_size = wintypes.DWORD()
                data = ctypes.c_void_p()
                opcode_type = ctypes.c_int()
                rc = wlanapi.WlanQueryInterface(
                    handle, ctypes.byref(item.InterfaceGuid), 7,  # current_connection
                    None, ctypes.byref(data_size), ctypes.byref(data),
                    ctypes.byref(opcode_type),
                )
                if rc != 0 or not data:
                    continue
                try:
                    attrs = ctypes.cast(data, ctypes.POINTER(WLAN_CONNECTION_ATTRIBUTES)).contents
                    assoc = attrs.wlanAssociationAttributes
                    ssid_len = min(assoc.dot11Ssid.uSSIDLength, 32)
                    raw = bytes(assoc.dot11Ssid.ucSSID[:ssid_len])
                    ssid = raw.decode("utf-8", "replace")
                    quality = int(assoc.wlanSignalQuality)

                    rssi = None
                    data2 = ctypes.c_void_p()
                    size2 = wintypes.DWORD()
                    rc2 = wlanapi.WlanQueryInterface(
                        handle, ctypes.byref(item.InterfaceGuid), 0x10000102,  # rssi
                        None, ctypes.byref(size2), ctypes.byref(data2),
                        ctypes.byref(opcode_type),
                    )
                    if rc2 == 0 and data2 and size2.value >= 4:
                        rssi = int(ctypes.cast(data2, ctypes.POINTER(wintypes.ULONG)).contents.value)
                        wlanapi.WlanFreeMemory(data2)

                    low = ssid.lower()
                    score = 0
                    if any(k in low for k in ("endoscope", "itimo", "mz", "soulear")):
                        score = 2
                    elif timeout_ssid:
                        score = 1
                    candidate = (score, ssid, quality, rssi)
                    if best is None or candidate[0] > best[0]:
                        best = candidate
                finally:
                    wlanapi.WlanFreeMemory(data)
            if best is None:
                return None, None, None, None
            _, ssid, quality, rssi = best
            bars = _rssi_to_bars(rssi)
            if bars is None:
                bars = _quality_to_bars(quality)
            return bars, rssi, quality, ssid
        finally:
            wlanapi.WlanFreeMemory(info_ptr)
    finally:
        wlanapi.WlanCloseHandle(handle, None)


if __name__ == "__main__":
    print(get_signal())
