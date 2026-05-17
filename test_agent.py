"""Validate agent output against SF UI screenshot data."""
from sf_agent import run

if __name__ == "__main__":
    # Match the screenshot: hires in 2025 (demo data range), with name + gender
    df = run(
        "Pull all employees hired between 2025-01-01 and 2025-12-31. "
        "Show: personIdExternal, userId, full employee name (firstName + lastName), "
        "gender, and hire date (startDate). Export to Excel."
    )
    print(f"\nDataFrame shape: {df.shape}")
    print(f"Columns: {list(df.columns)}")
    if not df.empty:
        print("\nSample rows (matching SF screenshot):")
        print(df.to_string(index=False))
