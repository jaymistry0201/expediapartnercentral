import json

def merge_data():
    print("Loading data...")
    with open("reservations_output.json", "r") as f:
        reservations = json.load(f)
        
    with open("details_output.json", "r") as f:
        details = json.load(f)

    print(f"Loaded {len(reservations)} reservations and {len(details)} details.")

    # Create a mapping of details using the Hotel confirmation code
    details_map = {d.get("Hotel confirmation code"): d for d in details if d and d.get("Hotel confirmation code")}

    merged_data = {}
    
    # If the file is already a dict (because we ran this before), reservations might be a dict. Handle both:
    if isinstance(reservations, dict):
        res_list = list(reservations.values())
    else:
        res_list = reservations

    for res in res_list:
        conf = res.get("Confirmation")
        if not conf:
            continue
            
        detail = details_map.get(conf, {})
        
        # Combine the basic info and detailed info
        merged_res = res.copy()
        merged_res.update(detail)
        
        # Save it into the final dictionary keyed by the confirmation number
        merged_data[conf] = merged_res

    print(f"Merged {len(merged_data)} records successfully.")

    with open("reservations_output.json", "w") as f:
        json.dump(merged_data, f, indent=4)
        
    print("Saved combined data back to reservations_output.json!")

if __name__ == "__main__":
    merge_data()
