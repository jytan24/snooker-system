from ultralytics import YOLO
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
    """Train YOLOv11s on the snooker_v2 dataset, validate, test, and export to ONNX."""
    device, device_arg, backend = get_compute_device()

    model = YOLO('yolo11s.pt')
    try:
        model.model.to(device)
    except Exception as e:
        print(f"Warning: could not move model tensors to {device}. Reason: {e}")
        device = torch.device('cpu')
        device_arg = 'cpu'
        backend = 'cpu'
        model.model.to(device)

    print(f"Starting training for V2 dataset on backend: {backend}...")
    
    try:
        try:
            results = model.train(
                data='data_v2.yaml',
                epochs=70,
                imgsz=960,
                device=device_arg,
                project='snooker_project',
                name='yolo11_snooker_v2',
            )
        except Exception as train_err:
            if backend == 'directml':
                print(f"DirectML training failed ({train_err}). Retrying on CPU for safety...")
                device = torch.device('cpu')
                device_arg = 'cpu'
                model.model.to(device)
                results = model.train(
                    data='data_v2.yaml',
                    epochs=70,
                    imgsz=960,
                    device=device_arg,
                    project='snooker_project',
                    name='yolo11_snooker_v2',
                )
            else:
                raise
        print("Training complete.")
        
        print("Starting validation...")
        metrics = model.val()
        print(f"mAP50-95: {metrics.box.map}")

        print("Starting testing on unseen data...")
        test_results = model.val(split='test')
        print(f"Test Set mAP50-95: {test_results.box.map}")

        model.export(format='onnx')

    except Exception as e:
        print(f"An error occurred: {e}")

if __name__ == '__main__':
    main()
