from ultralytics import YOLO
import os

model_path = 'C:/Snooker_AI_Project/runs/detect/Snooker_Results/v1_color_enhanced4/weights/best.pt'
model = YOLO(model_path)


username = os.getlogin()
target_image = f'C:/Users/{username}/Downloads/snooker.png' 

results = model.predict(source=target_image, save=True, imgsz=640, conf=0.5)

print(f"{results[0].save_dir}")