# DDS troubleshooting (CycloneDDS)

Config: `configs/cyclonedds.xml`. Unicast-only discovery: processes on one machine meet over `lo`,
other machines are listed as peers (`PI_IP`). Set in `.env`:

| Variable | Value |
|---|---|
| `DDS_IFACE` | Interface on the robot/Pi LAN: `ip route get 1.1.1.1`, the word after `dev` (workstation: `wlp7s0`) |
| `PI_IP` | The Pi's IP on that LAN |
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
| `failed to increase socket receive buffer size` | Harmless for small topics. For camera images raise the host limit: `sudo sysctl -w net.core.rmem_max=8388608` |
