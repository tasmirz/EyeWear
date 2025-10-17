import errno
import os
from multiprocessing import Process, Pool, Queue
from multiprocessing.managers import BaseManager
from typing import Dict, Any
import json
from pathlib import Path

request_queue = Queue()
result_queue = Queue()


class QueueManager(BaseManager): ...


def _get_request_queue():
    return request_queue


def _get_result_queue():
    return result_queue


QueueManager.register('get_queue', callable=_get_request_queue)
QueueManager.register('out_queue', callable=_get_result_queue)


def _start_or_connect_manager(address: tuple[str, int], authkey: bytes) -> tuple[BaseManager, bool]:
    """Start a manager if the address is free; otherwise connect to the existing one."""
    server = QueueManager(address=address, authkey=authkey)
    try:
        server.start()
        return server, True
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
    client = QueueManager(address=address, authkey=authkey)
    client.connect()
    return client, False


import json
from typing import Dict, List, Any, Tuple
from pathlib import Path
import numpy as np


class OCRToHTML:
    """
    Advanced OCR to HTML converter that handles multi-column documents,
    academic papers, and complex layouts.
    """
    
    def __init__(self, ocr_data: Dict[str, Any]):
        self.ocr_data = ocr_data
        self.results = ocr_data.get('result', [])
        self.text = ocr_data.get('text', '')
        
    def get_word_position(self, word_data: Dict) -> Tuple[float, float, float, float]:
        """Extract bounding box coordinates (x, y, width, height)."""
        poly = word_data.get('poly', [])
        if not poly or len(poly) < 4:
            return (0, 0, 0, 0)
        
        xs = [p[0] for p in poly]
        ys = [p[1] for p in poly]
        
        x_min, x_max = min(xs), max(xs)
        y_min, y_max = min(ys), max(ys)
        
        return (x_min, y_min, x_max - x_min, y_max - y_min)
    
    def detect_columns(self, threshold: float = 50) -> List[List[Dict]]:
        """
        Detect columns in the document by analyzing word positions.
        Returns list of columns, each containing words.
        """
        if not self.results:
            return [[]]
        
        # Get x-coordinates of all words
        word_positions = []
        for word in self.results:
            x, y, w, h = self.get_word_position(word)
            word_positions.append({
                'word': word,
                'x': x,
                'y': y,
                'x_center': x + w/2,
                'y_center': y + h/2,
                'width': w,
                'height': h
            })
        
        if not word_positions:
            return [[]]
        
        # Sort by x-coordinate
        sorted_by_x = sorted(word_positions, key=lambda w: w['x_center'])
        
        # Find column boundaries using x-coordinate clustering
        columns = []
        current_column = [sorted_by_x[0]]
        
        for i in range(1, len(sorted_by_x)):
            curr = sorted_by_x[i]
            prev = sorted_by_x[i-1]
            
            # If large horizontal gap, start new column
            x_gap = curr['x'] - (prev['x'] + prev['width'])
            
            if x_gap > threshold:
                columns.append(current_column)
                current_column = [curr]
            else:
                current_column.append(curr)
        
        if current_column:
            columns.append(current_column)
        
        # Sort words within each column by y-coordinate
        for col in columns:
            col.sort(key=lambda w: w['y_center'])
        
        return columns
    
    def detect_paragraphs(self, words: List[Dict], y_threshold: float = 20) -> List[List[Dict]]:
        """
        Detect paragraph breaks within a column based on vertical spacing.
        """
        if not words:
            return [[]]
        
        paragraphs = []
        current_para = [words[0]]
        
        for i in range(1, len(words)):
            curr = words[i]
            prev = words[i-1]
            
            # Calculate vertical gap
            y_gap = curr['y'] - (prev['y'] + prev['height'])
            
            # If significant vertical gap, start new paragraph
            if y_gap > y_threshold:
                paragraphs.append(current_para)
                current_para = [curr]
            else:
                current_para.append(curr)
        
        if current_para:
            paragraphs.append(current_para)
        
        return paragraphs
    
    def detect_lines(self, words: List[Dict], y_tolerance: float = 5) -> List[List[Dict]]:
        """
        Group words into lines based on y-coordinate proximity.
        """
        if not words:
            return [[]]
        
        lines = []
        current_line = [words[0]]
        
        for i in range(1, len(words)):
            curr = words[i]
            prev = words[i-1]
            
            # Check if words are on same line (similar y-coordinate)
            y_diff = abs(curr['y_center'] - prev['y_center'])
            
            if y_diff <= y_tolerance:
                current_line.append(curr)
            else:
                # Sort line by x-coordinate
                current_line.sort(key=lambda w: w['x_center'])
                lines.append(current_line)
                current_line = [curr]
        
        if current_line:
            current_line.sort(key=lambda w: w['x_center'])
            lines.append(current_line)
        
        return lines
    
    def is_heading(self, words: List[Dict]) -> bool:
        """
        Heuristic to detect if a line is likely a heading.
        Based on: shorter length, font size (if available), capitalization.
        """
        if not words:
            return False
        
        # Check if line is short (potential heading)
        text = ' '.join([w['word'].get('text', '') for w in words])
        
        # Heuristics
        is_short = len(text) < 50
        is_caps = text.isupper() or (text[0].isupper() if text else False)
        has_few_words = len(words) < 8
        
        return is_short and (is_caps or has_few_words)
    
    def generate_html(self, output_file: str = None) -> str:
        """Generate HTML with proper document structure."""
        
        # Detect columns
        columns = self.detect_columns()
        
        html_content = []
        total_words = len(self.results)
        total_columns = len(columns)
        languages = set(word.get('lang') for word in self.results if word.get('lang'))
        
        # Process each column
        for col_idx, column in enumerate(columns):
            html_content.append(f'<div class="column" data-column="{col_idx + 1}">')
            
            # Detect paragraphs in column
            paragraphs = self.detect_paragraphs(column)
            
            for para_idx, paragraph in enumerate(paragraphs):
                # Detect lines in paragraph
                lines = self.detect_lines(paragraph)
                
                # Check if paragraph is a heading
                if len(lines) == 1 and self.is_heading(lines[0]):
                    html_content.append('<h2 class="ocr-heading">')
                else:
                    html_content.append('<p class="ocr-paragraph">')
                
                # Add lines
                for line_idx, line in enumerate(lines):
                    if line_idx > 0:
                        html_content.append('<br>')
                    
                    html_content.append('<span class="ocr-line">')
                    
                    for word_data in line:
                        word = word_data['word']
                        text = word.get('text', '')
                        lang = word.get('lang', '')
                        poly = word.get('poly', [])
                        
                        lang_attr = f' data-lang="{lang}"' if lang else ''
                        poly_attr = f' data-poly="{json.dumps(poly)}"' if poly else ''
                        
                        html_content.append(
                            f'<span class="ocr-word"{lang_attr}{poly_attr}>{text}</span> '
                        )
                    
                    html_content.append('</span>')
                
                if len(lines) == 1 and self.is_heading(lines[0]):
                    html_content.append('</h2>')
                else:
                    html_content.append('</p>')
            
            html_content.append('</div>')
        
        # Statistics
        lang_list = ', '.join(sorted(languages)) if languages else 'N/A'
        
        # Complete HTML template
        html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OCR Document</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: 'Georgia', 'Times New Roman', serif;
            background: #f5f5f5;
            padding: 2rem;
            color: #333;
        }}

        .container {{
            max-width: 1400px;
            margin: 0 auto;
            background: white;
            box-shadow: 0 2px 10px rgba(0,0,0,0.1);
        }}

        .header {{
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 2rem;
        }}

        .header h1 {{
            font-size: 2rem;
            margin-bottom: 1rem;
        }}

        .stats {{
            display: flex;
            gap: 2rem;
            font-size: 0.9rem;
            opacity: 0.9;
        }}

        .stats-item {{
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }}

        .stats-item strong {{
            font-weight: 600;
        }}

        .document {{
            display: flex;
            gap: 2rem;
            padding: 3rem;
        }}

        .column {{
            flex: 1;
            min-width: 0;
        }}

        .column[data-column="2"] {{
            border-left: 1px solid #e0e0e0;
            padding-left: 2rem;
        }}

        .ocr-paragraph {{
            margin-bottom: 1.5rem;
            text-align: justify;
            line-height: 1.8;
            font-size: 1rem;
        }}

        .ocr-heading {{
            font-size: 1.4rem;
            font-weight: 700;
            margin: 2rem 0 1rem 0;
            color: #667eea;
            line-height: 1.4;
        }}

        .ocr-line {{
            display: inline;
        }}

        .ocr-word {{
            position: relative;
            cursor: default;
            transition: background-color 0.2s;
        }}

        .ocr-word:hover {{
            background-color: rgba(102, 126, 234, 0.1);
            border-radius: 2px;
        }}

        .ocr-word[data-lang]:hover::after {{
            content: attr(data-lang);
            position: absolute;
            bottom: 100%;
            left: 50%;
            transform: translateX(-50%);
            background: #333;
            color: white;
            padding: 0.3rem 0.6rem;
            border-radius: 4px;
            font-size: 0.7rem;
            white-space: nowrap;
            z-index: 100;
            margin-bottom: 0.5rem;
            font-family: 'Segoe UI', sans-serif;
        }}

        .column-indicator {{
            position: sticky;
            top: 0;
            background: #f0f4ff;
            padding: 0.5rem 1rem;
            margin: -1rem -1rem 1rem -1rem;
            border-radius: 4px;
            font-size: 0.85rem;
            color: #667eea;
            font-weight: 600;
            text-align: center;
        }}

        @media (max-width: 1024px) {{
            .document {{
                flex-direction: column;
            }}

            .column[data-column="2"] {{
                border-left: none;
                border-top: 1px solid #e0e0e0;
                padding-left: 0;
                padding-top: 2rem;
            }}
        }}

        @media print {{
            body {{
                background: white;
                padding: 0;
            }}
            
            .container {{
                box-shadow: none;
            }}
            
            .header {{
                background: #667eea;
            }}

            .column-indicator {{
                display: none;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <h1>📄 OCR Document Analysis</h1>
            <div class="stats">
                <div class="stats-item">
                    <strong>Columns:</strong> <span>{total_columns}</span>
                </div>
                <div class="stats-item">
                    <strong>Words:</strong> <span>{total_words}</span>
                </div>
                <div class="stats-item">
                    <strong>Languages:</strong> <span>{lang_list}</span>
                </div>
            </div>
        </div>
        
        <div class="document">
            {''.join(html_content)}
        </div>
    </div>
</body>
</html>"""
        
        # Save to file if specified
        if output_file:
            Path(output_file).write_text(html_template, encoding='utf-8')
            print(f"✅ HTML saved to: {output_file}")
        
        return html_template


def ocr_to_html2(ocr_data: Dict[str, Any], output_file: str = "output.html") -> str:
    """
    Convenience function to convert OCR data to HTML.
    
    Args:
        ocr_data: Dictionary containing 'text' and 'result' keys
        output_file: Path to save the HTML file
    
    Returns:
        HTML string
    """
    converter = OCRToHTML(ocr_data)
    return converter.generate_html(output_file)


def ocr_to_html(ocr_data: Dict[str, Any], output_file: str = "output.html") -> str:
    """
    Convert OCR dictionary to HTML while preserving paragraphs and line structure.
    
    Args:
        ocr_data: Dictionary containing 'text' and 'result' keys
        output_file: Path to save the HTML file (optional)
    
    Returns:
        HTML string
    """
    
    if not isinstance(ocr_data, dict) or 'result' not in ocr_data:
        raise ValueError("Invalid OCR data format. Expected dictionary with 'result' key.")
    
    results = ocr_data.get('result', [])
    
    # Group words by line number
    lines = {}
    languages = set()
    
    for item in results:
        line_no = item.get('line_no', 1)
        if line_no not in lines:
            lines[line_no] = []
        lines[line_no].append(item)
        
        if 'lang' in item:
            languages.add(item['lang'])
    
    # Sort lines and words
    sorted_line_nos = sorted(lines.keys())
    
    # Build HTML content
    html_lines = []
    
    for line_no in sorted_line_nos:
        words = sorted(lines[line_no], key=lambda x: x.get('word_no', 0))
        line_html = '<div class="ocr-line">'
        
        for word in words:
            text = word.get('text', '')
            lang = word.get('lang', '')
            poly = word.get('poly', [])
            
            lang_attr = f' data-lang="{lang}"' if lang else ''
            poly_attr = f' data-poly="{json.dumps(poly)}"' if poly else ''
            
            line_html += f'<span class="ocr-word"{lang_attr}{poly_attr}>{text}</span> '
        
        line_html += '</div>'
        html_lines.append(line_html)
    
    # Statistics
    total_words = len(results)
    total_lines = len(lines)
    lang_list = ', '.join(sorted(languages)) if languages else 'N/A'
    
    # Complete HTML template
    html_template = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>OCR Result</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 2rem;
        }}

        .container {{
            max-width: 900px;
            margin: 0 auto;
            background: white;
            border-radius: 12px;
            padding: 2.5rem;
            box-shadow: 0 10px 40px rgba(0,0,0,0.2);
        }}

        h1 {{
            color: #667eea;
            margin-bottom: 1.5rem;
            font-size: 2rem;
            border-bottom: 3px solid #667eea;
            padding-bottom: 0.5rem;
        }}

        .stats {{
            background: #f0f4ff;
            border-left: 4px solid #667eea;
            padding: 1rem;
            border-radius: 4px;
            margin-bottom: 2rem;
            font-size: 0.9rem;
            color: #555;
        }}

        .stats strong {{
            color: #667eea;
        }}

        .content {{
            line-height: 1.8;
            font-size: 1.1rem;
            color: #333;
        }}

        .ocr-line {{
            margin-bottom: 0.8rem;
            transition: background-color 0.2s;
            padding: 0.3rem 0;
        }}

        .ocr-line:hover {{
            background-color: rgba(102, 126, 234, 0.05);
            padding-left: 0.5rem;
            border-radius: 4px;
        }}

        .ocr-word {{
            position: relative;
            cursor: default;
        }}

        .ocr-word[data-lang]:hover {{
            background-color: rgba(102, 126, 234, 0.1);
            padding: 0.1rem 0.2rem;
            border-radius: 3px;
        }}

        .ocr-word[data-lang]:hover::after {{
            content: attr(data-lang);
            position: absolute;
            bottom: 100%;
            left: 50%;
            transform: translateX(-50%);
            background: #333;
            color: white;
            padding: 0.3rem 0.6rem;
            border-radius: 4px;
            font-size: 0.75rem;
            white-space: nowrap;
            z-index: 10;
            margin-bottom: 0.3rem;
        }}

        .ocr-word[data-lang]:hover::before {{
            content: '';
            position: absolute;
            bottom: 100%;
            left: 50%;
            transform: translateX(-50%);
            border: 5px solid transparent;
            border-top-color: #333;
            z-index: 10;
        }}

        @media print {{
            body {{
                background: white;
                padding: 0;
            }}
            
            .container {{
                box-shadow: none;
            }}
            
            .stats {{
                display: none;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>📄 OCR Document</h1>
        
        <div class="stats">
            <strong>Total Lines:</strong> {total_lines} | 
            <strong>Total Words:</strong> {total_words} | 
            <strong>Languages:</strong> {lang_list}
        </div>
        
        <div class="content">
            {chr(10).join(html_lines)}
        </div>
    </div>
</body>
</html>"""
    
    # Save to file if specified
    if output_file:
        Path(output_file).write_text(html_template, encoding='utf-8')
        print(f"HTML saved to: {output_file}")
    
    return html_template

def ocr_dict_to_html(ocr_dict):
    html = "<!DOCTYPE html>\n<html>\n<head>\n<meta charset='UTF-8'>\n<title>OCR Results</title>\n</head>\n<body>\n"
    
    html += "<h1>Full OCR Text</h1>\n<pre>{}</pre>\n".format(ocr_dict.get("text", ""))
    
    html += "<h2>Detailed OCR Data</h2>\n"
    
    # Group words by line_no
    lines = {}
    for word_info in ocr_dict.get("result", []):
        line_no = word_info.get("line_no", 0)
        if line_no not in lines:
            lines[line_no] = []
        lines[line_no].append(word_info)
    
    # Generate HTML
    for line_no in sorted(lines.keys()):
        html += f"<h3>Line {line_no}</h3>\n"
        html += "<ul>\n"
        for word in sorted(lines[line_no], key=lambda w: w['word_no']):
            html += "<li>Word {}: {} (Lang: {}, Poly: {})</li>\n".format(
                word.get("word_no", ""), word.get("text", ""), word.get("lang", ""), word.get("poly", "")
            )
        html += "</ul>\n"
    
    html += "</body>\n</html>"
    return html

def main() -> None:
    in_host = os.getenv("ASPIS_QUEUE_HOST", "127.0.0.1")
    in_port = int(os.getenv("ASPIS_QUEUE_PORT", "50000"))
    out_host = os.getenv("ASPIS_OUT_HOST", "127.0.0.1")
    out_port = int(os.getenv("ASPIS_OUT_PORT", "50001"))

    try:
        manager, owns_manager = _start_or_connect_manager((in_host, in_port), b'abcf')
    except OSError as exc:
        raise RuntimeError(
            f"Unable to bind request queue to {in_host}:{in_port}: {exc}. "
            "Is another pipeline already running?"
        ) from exc

    try:
        out_manager, owns_out_manager = _start_or_connect_manager((out_host, out_port), b'abcfe')
    except OSError as exc:
        if owns_manager:
            manager.shutdown()
        raise RuntimeError(
            f"Unable to bind outbound queue to {out_host}:{out_port}: {exc}."
        ) from exc

    out_queue = out_manager.out_queue()
    job_queue = manager.get_queue()

    try:
        while True:
            print("Waiting for new filename on queue...")
            file_path = job_queue.get()
            print(f"Received filename: {file_path}")
            from apsisocr import ApsisOCR
            ocr = ApsisOCR()
            results = ocr(file_path)

            file_stem = Path(file_path).stem

            ocr_to_html2(results, "academic_paper.html")
            html_content = ocr_to_html(results)
            output_path = Path('./save/html') / f'{file_stem}.html'
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(html_content, encoding='utf-8')

            out_queue.put(str(output_path))
    except KeyboardInterrupt:
        print("Interrupted, exiting.")
    finally:
        if owns_manager:
            manager.shutdown()
        if owns_out_manager:
            out_manager.shutdown()


if __name__ == "__main__":
    main()
