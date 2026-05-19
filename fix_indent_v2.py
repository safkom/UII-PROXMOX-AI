import sys

def fix_routes():
    path = "backend/api/routes.py"
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    
    # Target the specific problematic block
    old_fragment = """                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    continue

                    response_piece = data.get("response", "")"""
    
    new_fragment = """                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    continue

                response_piece = data.get("response", "")"""
    
    if old_fragment in content:
        new_content = content.replace(old_fragment, new_fragment)
        with open(path, "w", encoding="utf-8") as f:
            f.write(new_content)
        print("Successfully fixed indentation.")
    else:
        # Try finding it without the extra newline if it's different
        old_fragment_2 = """                try:
                    data = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                    response_piece = data.get("response", "")"""
        if old_fragment_2 in content:
            new_content = content.replace(old_fragment_2, new_fragment)
            with open(path, "w", encoding="utf-8") as f:
                f.write(new_content)
            print("Successfully fixed indentation (alt match).")
        else:
            print("Could not find the target code fragment.")

if __name__ == "__main__":
    fix_routes()
