import os
import json

print("Opening dataset...")

filename = "Entity Recognition in Resumes.json"

skills = set()
total = 0

with open(filename, "r", encoding="utf-8") as f:
    for line in f:
        line = line.strip()
        if not line:
            continue

        item = json.loads(line)
        total += 1

        if "annotation" not in item:
            continue

        for ann in item["annotation"]:

            # --- SAFE LABEL HANDLING ---
            label_list = ann.get("label", [])
            if not label_list:
                continue

            label = label_list[0].lower()

            if "skill" not in label:
                continue

            for point in ann.get("points", []):
                start = point.get("start", None)
                end = point.get("end", None)

                if start is None or end is None:
                    continue

                skill = item["content"][start:end+1].strip().lower()

                if len(skill) > 1:
                    skills.add(skill)

print("Total resumes loaded:", total)
print("Total extracted skills:", len(skills))

with open("skills_db.txt", "w", encoding="utf-8") as f:
    for s in sorted(skills):
        f.write(s + "\n")

print("skills_db.txt created successfully!")
