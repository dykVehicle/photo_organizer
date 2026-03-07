---
license: mit
pipeline_tag: image-classification
datasets:
- monkt/doctype
---

# DocType - Document Image Classification

A high-performance MobileNetV3-based document classifier that categorizes document images into 7 distinct types. Optimized for production deployment with ONNX format.

## 🎯 Model Overview

This model classifies document images into the following categories:

| Category | Description |
|----------|-------------|
| **chart** | Charts, graphs, and data visualizations |
| **diagram** | Flowcharts, diagrams, and technical drawings |
| **document_handwritten** | Handwritten documents and notes |
| **document_printed** | Printed text documents |
| **map** | Maps and geographic visualizations |
| **photo** | Photographs and natural images |
| **screenshot** | Screenshots and screen captures |


## 🚀 Performance

### Model Metrics

- **Architecture**: MobileNetV3-Large (transfer learning + fine-tuning)
- **Input Size**: 320×320 pixels
- **Parameters**: ~5.4M (lightweight and efficient)
- **Inference Time**: ~10-30ms on CPU (depending on hardware)

### Training Details

- **Dataset Size**: 21,000 images (17,500 train / 2,100 val / 1,400 test)
- **Training Strategy**: 
  - Phase 1: Transfer learning with frozen base (40 epochs)
  - Phase 2: Fine-tuning entire model (20 epochs)
- **Data Augmentation**: Rotation, shifts, zoom, brightness variation
- **Optimizer**: Adam (lr=0.001 → 1e-5 for fine-tuning)

## 📮 Citation

If you use this model in your research or project, please cite.