from ultralytics import YOLO
import os
import torch


def get_compute_device():
    """Return (torch_device, device_arg, backend_name) with DirectML->CPU fallback."""
    try:
        import torch_directml  # Optional dependency on Windows

        dml_device = torch_directml.device()
        # Sanity check that tensor allocation works on this device
        _ = torch.zeros((1,), device=dml_device)
        print(f"Using DirectML device: {dml_device}")
        return dml_device, str(dml_device), 'directml'
    except Exception as e:
        cpu_device = torch.device('cpu')
        print(f"DirectML not available. Falling back to CPU. Reason: {e}")
        return cpu_device, 'cpu', 'cpu'

def main():
    """Load a trained YOLO model and run inference on images in new_test_images/."""
    device, device_arg, backend = get_compute_device()

    model_path = 'runs/detect/snooker_project/yolo11_snooker_v2/weights/best.pt'
    new_folder_path = 'new_test_images'

    print(f"Loading model from: {model_path}")
    print(f"Testing on images in: {new_folder_path}")

    if not os.path.exists(model_path):
        print("Error: Model weights not found. Check the path.")
        return

    model = YOLO(model_path)
    try:
        model.model.to(device)
    except Exception as e:
        print(f"Warning: could not move model tensors to {device}. Reason: {e}")
        device = torch.device('cpu')
        device_arg = 'cpu'
        backend = 'cpu'
        model.model.to(device)

    print(f"Running prediction on backend: {backend}")
    try:
        results = model.predict(source=new_folder_path, save=True, conf=0.25, device=device_arg)
    except Exception as pred_err:
        if backend == 'directml':
            print(f"DirectML inference failed ({pred_err}). Retrying on CPU for safety...")
            device = torch.device('cpu')
            device_arg = 'cpu'
            model.model.to(device)
            results = model.predict(source=new_folder_path, save=True, conf=0.25, device=device_arg)
        else:
            raise

    if results:
        print("\nPrediction complete!")
        print(f"Results saved to: {results[0].save_dir}")
    else:
        print("\nNo predictions were made (is the folder empty?)")

if __name__ == '__main__':
    main()
