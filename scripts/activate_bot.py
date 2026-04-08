import os
import json
from dotenv import load_dotenv
from py_clob_client.client import ClobClient
from py_clob_client.clob_types import ApiCreds

def main():
    print("--- Polymarket Bot Activation Tool ---\n")
    
    # 1. Load .env
    load_dotenv()
    pk = os.getenv('POLY_PRIVATE_KEY')
    addr = os.getenv('POLY_WALLET_ADDRESS')
    
    if not pk:
        print("❌ ERROR: POLY_PRIVATE_KEY not found in .env!")
        return

    print(f"✅ Found Private Key for Wallet: {addr}")
    
    try:
        # 2. Initialize Client
        client = ClobClient('https://clob.polymarket.com', key=pk, chain_id=137, funder=addr)
        
        # 3. Generate Keys
        print("🔄 Activating Trading Keys (Generating/Deriving)...")
        # Use derived address to be 100% sure
        derived_addr = client.get_address()
        creds = client.create_or_derive_api_creds()
        
        # 4. RE-INITIALIZE with full credentials
        client = ClobClient('https://clob.polymarket.com', key=pk, chain_id=137, creds=creds, funder=derived_addr)
        
        print("\n" + "="*30)
        print("🔑 SUCCESS! COPY THESE INTO YOUR .env FILE:")
        print("="*30)
        print(f"POLY_API_KEY={creds.api_key}")
        print(f"POLY_API_SECRET={creds.api_secret}")
        print(f"POLY_API_PASSPHRASE={creds.api_passphrase}")
        print("="*30)
        
        # 5. Authorize USDC
        print("\n🔄 Authorizing USDC for trading...")
        from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
        try:
            # Check balance first for better UX
            print(f"🔍 Checking USDC balance for {addr}...")
            # Using collateral asset type for USDC
            params = BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            
            # Update allowance to maximum (standard for trading bots)
            resp = client.update_balance_allowance(params)
            print(f"✅ Authorization/Allowance Success: {resp}")
            
            print("\n" + "-"*30)
            print("💎 WALLET STATUS:")
            print(f"Address: {addr}")
            print("Network: Polygon (Mainnet)")
            print("Asset: USDC")
            print("-"*30)
            print("\n👉 IF YOU GET 'NOT ENOUGH BALANCE' LATER:")
            print("Add at least 5-10 USDC to the address above.")
            print("-" * 30)

        except Exception as e:
            if "not enough balance" in str(e).lower() or "insufficient funds" in str(e).lower():
                print("\n⚠️  WARNING: Could not set allowance because your USDC balance is 0.")
                print(f"👉 Please send some USDC (Polygon Network) to {addr} and then run this script again.")
            else:
                print(f"❌ Authorization Error: {e}")
                
        print("\n🚀 Ready! To start the bot, run: .\\start_local.ps1")

    except Exception as e:
        print(f"❌ CRITICAL ERROR: {e}")

if __name__ == "__main__":
    main()
