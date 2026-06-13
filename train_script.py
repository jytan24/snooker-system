from ultralytics import YOLO

if __name__ == '__main__':
    model = YOLO('yolov8s.pt') 

    model.train(
        data='./datasets/data.yaml',
        epochs=100,
        imgsz=640,
        
        # --- 颜色防混淆参数保持不变 ---
        hsv_h=0.015,
        hsv_s=0.7,   
        hsv_v=0.4,   
        
        # --- 关键修改：针对 Error 1455 ---
        device=0,    
        workers=0,   # 改成 0，让主进程直接读图，不再通过共享内存
        batch=8,     # 如果显存还报 OOM，可以把 16 调小到 8 或 4
        
        project='Snooker_Results',
        name='v1_color_enhanced'
    )