from ultralytics import YOLO

def main():
    # 1. Load the YOLOv11 model (Small)
    model = YOLO('yolo11s.pt')

    print("Starting training for V2 dataset...")
    
    # 2. Train the model
    # Saving to a new project folder 'snooker_project' with name 'yolo11_snooker_v2'
    try:
        results = model.train(
            data='data_v2.yaml',    # Pointing to the NEW yaml
            epochs=70,
            imgsz=960,              # Standard size, change to 960 or 1280 if needed
            device='cpu',           # Change to '0' if using GPU
            project='snooker_project',
            name='yolo11_snooker_v2', # New run name
              
        )
        print("Training complete.")
        
        # 3. Validate (on 'val' set)
        print("Starting validation...")
        metrics = model.val()
        print(f"mAP50-95: {metrics.box.map}")

        # 4. Test (on 'test' set)
        print("Starting testing on unseen data...")
        test_results = model.val(split='test')
        print(f"Test Set mAP50-95: {test_results.box.map}")

        # 5. Export
        model.export(format='onnx')

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == '__main__':
    main()
