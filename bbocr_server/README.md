# Note

Configure .env file for models

Create these directories

```bash
 mkdir -p /save/data/roi
 mkdir -p save/data/roi
 mkdir -p save/data/predicted_text
 mkdir -p save/data/paragraph_wise_predicted_word_box
 mkdir -p save/data/word_gc_all_box_all_lines
 mkdir -p save/html
```

Download apisocr and paddleocr required models and place as such in your models folder
```
.
├── bnencls.onnx
├── bnocr.onnx
├── line
│   ├── inference.pdiparams
│   ├── inference.pdiparams.info
│   └── inference.pdmodel
├── svtr
│   ├── en_dict.txt
│   ├── inference.pdiparams
│   ├── inference.pdiparams.info
│   └── inference.pdmodel
└── word
    ├── inference.pdiparams
    ├── inference.pdiparams.info
    └── inference.pdmodel
```
