"""
Quick diagnostic to test if boss_config is loading correctly.
Run this from your project root with: python test_boss_config.py
"""
import sys
from pathlib import Path

# Add project to path
project_root = Path(__file__).parent
sys.path.insert(0, str(project_root))

from src import boss_config

print("="*60)
print("BOSS CONFIG DIAGNOSTIC")
print("="*60)

# Test 1: Load Manaforge Omega
print("\n1. Testing Manaforge_Omega.json")
try:
    ability_names = boss_config.get_ability_names("Manaforge_Omega.json")
    boss_options = boss_config.get_boss_options("Manaforge_Omega.json")
    print(f"   ✓ Loaded successfully")
    print(f"   ✓ Found {len(boss_options)} bosses")
    print(f"   ✓ Boss names: {list(boss_options.keys())[:3]}...")
    print(f"   ✓ Ability names loaded: {len(ability_names)}")
except Exception as e:
    print(f"   ✗ ERROR: {e}")

# Test 2: Load Midnight Season 1
print("\n2. Testing Midnight_season_1.json")
try:
    ability_names = boss_config.get_ability_names("Midnight_season_1.json")
    boss_options = boss_config.get_boss_options("Midnight_season_1.json")
    print(f"   ✓ Loaded successfully")
    print(f"   ✓ Found {len(boss_options)} bosses")
    print(f"   ✓ Boss names:")
    for boss_name in boss_options.keys():
        boss_info = boss_options[boss_name]
        print(f"      - {boss_name} (ID: {boss_info['id']}, {len(boss_info['abilities'])} abilities)")
    print(f"   ✓ Ability names loaded: {len(ability_names)}")
except Exception as e:
    print(f"   ✗ ERROR: {e}")
    import traceback
    traceback.print_exc()

# Test 3: Check file paths
print("\n3. Checking file paths")
config_dir = project_root / "config" / "bosses"
print(f"   Config directory: {config_dir}")
print(f"   Directory exists: {config_dir.exists()}")

for json_file in ["Manaforge_Omega.json", "Midnight_season_1.json"]:
    file_path = config_dir / json_file
    print(f"   {json_file}:")
    print(f"      - Exists: {file_path.exists()}")
    if file_path.exists():
        print(f"      - Size: {file_path.stat().st_size} bytes")

print("\n" + "="*60)
print("If Midnight Season 1 loaded successfully above, your")
print("boss_config.py is working correctly. The issue is likely")
print("that Streamlit needs to be restarted or session state cleared.")
print("="*60)
