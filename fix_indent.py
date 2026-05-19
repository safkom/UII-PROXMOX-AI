import sys

def fix_routes():
    path = "backend/api/routes.py"
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()
    
    new_lines = []
    i = 0
    while i < len(lines):
        line = lines[i]
        new_lines.append(line)
        # Check if this is the start of the problematic block
        if "except json.JSONDecodeError:" in line and "continue" in line:
            # Look ahead for lines that are incorrectly indented
            while i + 1 < len(lines):
                next_line = lines[i+1]
                # If it starts with 20 spaces (5 levels of 4), it might be inside the 'except'
                # but logically it should be outside if it's processing 'data'.
                # Actually, the 'continue' is on the same line or next line.
                # In the 'Get-Content' output, 'continue' was on its own line under 'except'.
                # The lines following it: response_piece = data.get("response", "")
                # had too much indentation (they seemed to be aligned with continue or deeper)
                # but they follow a 'continue', so they won't execute if 'continue' is hit.
                # But they are currently *inside* the 'except' block or after it?
                # Looking at the output again:
                #                   except json.JSONDecodeError:
                #                       continue
                #   
                #                       response_piece = data.get("response", "")
                
                if "response_piece = data.get" in next_line:
                    # Fix indentation of this and following lines until 'if data.get("done")'
                    j = i + 1
                    while j < len(lines) and 'if data.get("done"):' not in lines[j]:
                         # Reduce indentation by 4 spaces (one level) to pull it out of the except block
                         # and align it with 'try'
                         curr = lines[j]
                         if curr.strip():
                             new_lines.append(curr[4:] if curr.startswith("    ") else curr)
                         else:
                             new_lines.append(curr)
                         j += 1
                    i = j - 1
                    break
                else:
                    break
        i += 1
    
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)

if __name__ == "__main__":
    fix_routes()
