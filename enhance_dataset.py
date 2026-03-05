import os
import shutil
from PIL import Image, ImageEnhance

# --- CONFIGURATION ---
SOURCE_dataset = "datasets/snooker"         # Your current dataset
DEST_dataset   = "datasets/snooker_enhanced" # Where the new one will go

# 1.0 = Original. >1.0 = Increase. <1.0 = Decrease.
BRIGHTNESS_FACTOR = 1.2  # Make images 20% brighter
SHARPNESS_FACTOR  = 1.7  # Make images 70% sharper
# ---------------------

def enhance_and_save(src_path, dest_path):
    """Opens an image, enhances it, and saves it to destination."""
    try:
        with Image.open(src_path) as img:
            # 1. Adjust Brightness
            enhancer_b = ImageEnhance.Brightness(img)
            img = enhancer_b.enhance(BRIGHTNESS_FACTOR)
            
            # 2. Adjust Sharpness
            enhancer_s = ImageEnhance.Sharpness(img)
            img = enhancer_s.enhance(SHARPNESS_FACTOR)
            
            # Save
            img.save(dest_path)
    except Exception as e:
        print(f"Error processing {src_path}: {e}")

def main():
    if not os.path.exists(SOURCE_dataset):
        print(f"Error: Source folder '{SOURCE_dataset}' does not exist.")
        return

    print(f"Starting enhancement...")
    print(f"Source: {SOURCE_dataset}")
    print(f"Dest:   {DEST_dataset}")

    # Walk through all folders in the source dataset
    for root, dirs, files in os.walk(SOURCE_dataset):
        for filename in files:
            src_file_path = os.path.join(root, filename)
            
            # Create the corresponding path in the destination folder
            # Relpath gets the path piece starting after 'datasets/snooker'
            relative_path = os.path.relpath(src_file_path, SOURCE_dataset)
            dest_file_path = os.path.join(DEST_dataset, relative_path)
            
            # Create the folder if it doesn't exist
            os.makedirs(os.path.dirname(dest_file_path), exist_ok=True)
            
            # Check if it is an image
            if filename.lower().endswith(('.jpg', '.jpeg', '.png', '.bmp')):
                # Enhance the image
                enhance_and_save(src_file_path, dest_file_path)
            else:
                # If it's a label (.txt) or config (.yaml), just copy it exactly
                shutil.copy2(src_file_path, dest_file_path)

    print("\n-------------------------------------------")
    print("Success! Enhanced dataset created.")
    print(f"Location: {DEST_dataset}")
    print("-------------------------------------------")
    print("Next Steps:")
    print("1. Open the new folder and check if the images look better.")
    print("2. If yes, update your 'data.yaml' to point to '../datasets/snooker_enhanced'")

if __name__ == "__main__":
    main()
