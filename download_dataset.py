import kagglehub
import os
import shutil

# download dataset
path = kagglehub.dataset_download("dataturks/resume-entities-for-ner")
print("Dataset downloaded to:", path)

# search json files
found = False

for root, dirs, files in os.walk(path):
    for file in files:
        if file.lower().endswith(".json"):
            full_path = os.path.join(root, file)
            print("Found JSON:", full_path)

            shutil.copy(full_path, "Entity Recognition in Resumes.json")
            print("\nCopied as: Entity Recognition in Resumes.json")
            found = True
            break
    if found:
        break

if not found:
    print("No JSON file found! Check dataset manually.")
