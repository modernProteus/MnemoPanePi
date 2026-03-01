# MnemoPane

MnemoPane is a network-aware, BLE-accessible display system with:

- Always-on Admin UI
- BLE Super Admin Backdoor
- Smart WiFi / SoftAP fallback
- Modular service architecture

## Architecture Overview

bin/       → System services (executed by systemd)  
admin/     → Web Admin UI  
systemd/   → Service unit source-of-truth  
scripts/   → Installer & maintenance tools  
config/    → Runtime configuration  
docs/      → Bring-up & design documentation  

See docs/Phase1_Bringup.md for full recreation steps.