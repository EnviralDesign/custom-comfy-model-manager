"""
Script to test the remote agent handshake from WSL/Remote machine.
Usage:
    python test_handshake.py http://127.0.0.1:8420 <API_KEY>
"""

import sys
import time
import requests
import platform
import socket

def main():
    if len(sys.argv) < 3:
        print("Usage: python test_handshake.py <BASE_URL> <API_KEY>")
        sys.exit(1)

    base_url = sys.argv[1].rstrip('/')
    api_key = sys.argv[2]
    
    headers = {
        "Authorization": f"Bearer {api_key}"
    }

    print(f"--- Remote Agent Handshake Test ---")
    print(f"Target: {base_url}")
    print(f"Key:    {api_key[:8]}...")
    
    # 1. Register
    print("\n1. Registering Agent...")
    try:
        payload = {
            "hostname": socket.gethostname(),
            "os": f"{platform.system()} {platform.release()}",
            "details": {"python": platform.python_version()}
        }
        resp = requests.post(f"{base_url}/api/remote/agent/register", json=payload, headers=headers)
        
        if resp.status_code == 200:
            print("   ✅ Success: " + resp.text)
        else:
            print(f"   ❌ Failed ({resp.status_code}): {resp.text}")
            sys.exit(1)
            
    except Exception as e:
        print(f"   ❌ Connection Error: {e}")
        sys.exit(1)

    # 2. Heartbeat Loop
    print("\n2. Sending heartbeats (Ctrl+C to stop)...")
    try:
        while True:
            resp = requests.post(f"{base_url}/api/remote/agent/heartbeat", headers=headers)
            if resp.status_code == 200:
                print(f"   ❤️  {time.strftime('%H:%M:%S')} - OK", end='\r')
            else:
                print(f"\n   ❌ Heartbeat Failed ({resp.status_code})")
                break
            time.sleep(5)
    except KeyboardInterrupt:
        print("\nStopping.")

if __name__ == "__main__":
    main()
