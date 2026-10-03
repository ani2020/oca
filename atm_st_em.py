import numpy as np

def calculate_expected_moves(stock_price, iv, dte, straddle_price):
    """
    Calculates the 1-standard deviation expected move using 
    both the Straddle Heuristic and the Standard IV Formula.
    """
    # 1. Straddle Method
    em_straddle = straddle_price * 0.85
    
    # 2. Implied Volatility Method
    # T represents the time fraction of a standard 365-day calendar year
    t = dte / 365.0
    em_iv = stock_price * iv * np.sqrt(t)
    
    return {
        "straddle_method": {
            "move_dollars": round(em_straddle, 2),
            "lower_bound": round(stock_price - em_straddle, 2),
            "upper_bound": round(stock_price + em_straddle, 2)
        },
        "iv_method": {
            "move_dollars": round(em_iv, 2),
            "lower_bound": round(stock_price - em_iv, 2),
            "upper_bound": round(stock_price + em_iv, 2)
        }
    }

# --- Market Constraints & Inputs ---
spot = 500.00          # Current Underlying Asset Price
implied_vol = 0.25     # ATM Implied Volatility (25%)
days_to_exp = 30       # Days to Expiration (DTE)
atm_straddle = 22.50   # Market price of the front-month ATM Straddle

# Compute boundaries
moves = calculate_expected_moves(spot, implied_vol, days_to_exp, atm_straddle)

# Output formatting for verification
print(f"--- Expected Move Metrics for Spot ${spot} (DTE: {days_to_exp}) ---")
print(f"Straddle Method Expected Move: +/-${moves['straddle_method']['move_dollars']}")
print(f"Range: ${moves['straddle_method']['lower_bound']} to ${moves['straddle_method']['upper_bound']}\n")
print(f"IV Method Expected Move: +/-${moves['iv_method']['move_dollars']}")
print(f"Range: ${moves['iv_method']['lower_bound']} to ${moves['iv_method']['upper_bound']}")
