# DDS troubleshooting (CycloneDDS)

Config: `configs/cyclonedds.xml`. Unicast-only discovery: processes on one machine meet over `lo`,
other machines are listed as peers (`PI_IP`, and `BRIDGE_IP` on the Pi). Set in `.env`:

| Variable | Value |
|---|---|
| `DDS_IFACE` | Interface on the robot/Pi LAN: `ip route get 1.1.1.1`, the word after `dev` (workstation: `wlp7s0`) |
| `PI_IP` | The Pi's IP on that LAN |
| `BRIDGE_IP` | On the Pi only: the IP of the machine running bringup (workstation or laptop) |
| `ROS_DOMAIN_ID` | Same on every machine |

## Firewall: let the Pi in

The workstation runs ufw with deny-incoming, which drops DDS packets from other machines. Allow DDS
from the LAN on the DDS ports (domain 0, up to 100 participants per machine):

```bash
sudo ufw allow in on wlp7s0 from 192.168.0.0/24 to any port 7400:7650 proto udp comment 'ROS 2 DDS'
```

Other domains: ports start at `7400 + 250 * ROS_DOMAIN_ID`. The laptop needs the same rule if ufw is on
(`sudo ufw status`).

## Symptoms

| Symptom | Cause |
|---|---|
| Container exits with `DDS_IFACE is not set` | Set `DDS_IFACE` in `.env` |
| Nodes in one container don't see each other / `ros2` CLI hangs on exit | Multicast through a firewall. Make sure the container uses `configs/cyclonedds.xml` (`CYCLONEDDS_URI`) |
| `Failed to find a free participant index` | More than 100 ROS processes on one machine. Raise `MaxAutoParticipantIndex` (and the firewall port range) |
| Pi topics missing on the workstation | Wrong `PI_IP` / `DDS_IFACE`, different `ROS_DOMAIN_ID`, or the firewall rule above |
| Log floods with `ddsi_udp_conn_write to udp/<other machine>:74xx failed with retcode -3`, and the other machine's topics never show up | `lo` listed under `<Interfaces>` in `configs/cyclonedds.xml`: Cyclone then sends to other machines through loopback, which Linux refuses. Keep only `DDS_IFACE` there |
| Pi topics show up only after ~30 s, or not at all | `BRIDGE_IP` missing in the Pi's `.env`: the Pi then only finds the workstation when the workstation's periodic announcement reaches it |
| `failed to increase socket receive buffer size` | Harmless for small topics. For camera images raise the host limit: `sudo sysctl -w net.core.rmem_max=8388608` |
