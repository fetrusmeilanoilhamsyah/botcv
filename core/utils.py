import re
import os

def sanitize_filename(filename: str) -> str:
    r"""
    Menghapus karakter yang tidak diperbolehkan dalam nama file:
    \ / : * ? " < > |
    Serta mencegah path traversal dan membatasi panjang nama file.
    """
    if not filename:
        return "output"
    
    # Remove path components - cegah directory traversal
    filename = os.path.basename(filename)
    
    # Hapus .. dan . untuk extra safety
    filename = filename.replace("..", "").replace("./", "")
    
    # Hapus karakter ilegal
    filename = re.sub(r'[\\/:*?"<>|]', '', filename)
    
    # Trim whitespace
    filename = filename.strip()
    
    # Jika jadi kosong atau dimulai dengan . (hidden file)
    if not filename or filename.startswith('.'):
        return "output"
    
    # Batasi panjang (maks 100 karakter)
    return filename[:100]
