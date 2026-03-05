from ultralytics import YOLO
import os
import glob

def main():
    # 1. Define Paths
    # Update this path if your model is stored elsewhere
    model_path = 'runs/detect/snooker_project/yolo11_snooker_v2/weights/best.pt'
    
    # Define an image to test. 
    # Use the first image found in the NEW folder
    # Please put your images in the 'new_test_images' folder!
    new_folder_path = 'new_test_images'
    # test_images_files = glob.glob(os.path.join(new_folder_path, '*.jpg'))
    
    # if test_images_files:
    #     source_image = test_images_files[0] # Pick the first available image
    # else:
    #     print(f"No images found in {new_folder_path}. Please add a .jpg file there.")
    #     return

    print(f"Loading model from: {model_path}")
    print(f"Testing on images in: {new_folder_path}")

    if not os.path.exists(model_path):
        print("Error: Model weights not found. Check the path.")
        return

    # 2. Load the trained model
    model = YOLO(model_path)

    # 3. Run Inference (Prediction)
    # save=True will save the image with drawn boxes to 'runs/detect/predict'
    # conf=0.25 is the confidence threshold (25%)
    results = model.predict(source=new_folder_path, save=True, conf=0.25)

    if results:
        print("\nPrediction complete!")
        print(f"Results saved to: {results[0].save_dir}")
    else:
        print("\nNo predictions were made (is the folder empty?)")

if __name__ == '__main__':
    main()
