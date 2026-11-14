package com.fordownloads.earview.camera

import java.net.DatagramSocket
import java.net.Inet4Address
import java.net.InetAddress
import java.net.InetSocketAddress
import java.net.NetworkInterface

/** Local IPv4 discovery and socket binding helpers. */
object NetIf {

    /**
     * Local IPv4 addresses, addresses matching [prefix] first, loopback last.
     * Replaces the Python gethostbyname_ex() approach: it also finds addresses
     * on interfaces that are not in the default route.
     */
    fun candidates(prefix: String? = null): List<String> {
        val found = LinkedHashSet<String>()
        try {
            for (nif in NetworkInterface.getNetworkInterfaces()) {
                if (!nif.isUp || nif.isLoopback) continue
                for (address in nif.inetAddresses) {
                    if (address is Inet4Address && !address.isLoopbackAddress && !address.isLinkLocalAddress) {
                        address.hostAddress?.let { found.add(it) }
                    }
                }
            }
        } catch (_: Exception) {
        }
        val all = found.toList()
        if (prefix == null) return all + "127.0.0.1"
        return all.filter { it.startsWith(prefix) } +
            all.filterNot { it.startsWith(prefix) } +
            "127.0.0.1"
    }

    /** Datagram socket bound to a specific local IPv4 and port (0 = ephemeral). */
    fun openDatagram(localIp: String, port: Int, receiveBuffer: Int = 0): DatagramSocket {
        val socket = DatagramSocket(null)
        try {
            if (receiveBuffer > 0) socket.receiveBufferSize = receiveBuffer
            socket.bind(InetSocketAddress(InetAddress.getByName(localIp), port))
        } catch (e: Exception) {
            socket.close()
            throw e
        }
        return socket
    }
}
