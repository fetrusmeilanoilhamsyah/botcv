"""
txt_splitter.py
Pecah file TXT (berisi nomor HP, satu per baris) menjadi beberapa file TXT kecil.
Penamaan: PECAHAN1.txt, PECAHAN2.txt, ...
"""
import os


def split_txt(lines: list, output_dir: str, per_file: int) -> list:
    """
    Terima list baris (nomor HP), pecah menjadi file TXT berisi N nomor.
    Kembalikan list path file hasil pecahan, berurutan.
    """
    output_files = []
    chunk_num = 1

    for i in range(0, len(lines), per_file):
        chunk = lines[i:i + per_file]
        filename = f"PECAHAN{chunk_num}.txt"
        out_path = os.path.join(output_dir, filename)
        with open(out_path, "w", encoding="utf-8") as f:
            f.write("\n".join(chunk) + "\n")
        output_files.append(out_path)
        chunk_num += 1

    return output_files
