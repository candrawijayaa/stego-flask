from flask import Flask, render_template, request, send_file, flash, redirect, url_for, session
from PIL import Image
from io import BytesIO
import os

app = Flask(__name__, static_folder="static", template_folder="templates")
app.secret_key = os.urandom(24)
# Batasi upload ~20MB (sesuaikan kebutuhan)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024

########## LSB helpers ##########
def _int_to_bits(value: int, length: int):
    return [(value >> (length - 1 - i)) & 1 for i in range(length)]

def _bits_to_int(bits):
    value = 0
    for b in bits:
        value = (value << 1) | (b & 1)
    return value

def _bytes_to_bits(data: bytes):
    bits = []
    for byte in data:
        for i in range(8):
            bits.append((byte >> (7 - i)) & 1)
    return bits

def _bits_to_bytes(bits):
    b = bytearray()
    for i in range(0, len(bits), 8):
        byte_bits = bits[i:i+8]
        if len(byte_bits) < 8:
            byte_bits += [0] * (8 - len(byte_bits))
        b.append(_bits_to_int(byte_bits))
    return bytes(b)

def capacity_in_bits(img: Image.Image):
    w, h = img.size
    return w * h * 3

def embed_bytes_into_image_obj(img: Image.Image, data: bytes) -> Image.Image:
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")

    has_alpha = (img.mode == "RGBA")
    pixels = list(img.getdata())

    total_capacity = capacity_in_bits(img)
    data_len = len(data)
    needed_bits = 32 + data_len * 8
    if needed_bits > total_capacity:
        raise ValueError(f"Kapasitas tidak cukup. Butuh {needed_bits} bit, gambar hanya {total_capacity} bit.")

    header_bits = _int_to_bits(data_len, 32)
    data_bits = _bytes_to_bits(data)
    bitstream = header_bits + data_bits
    bit_iter = iter(bitstream)

    new_pixels = []
    for px in pixels:
        r, g, b = px[0], px[1], px[2]
        try:
            r = (r & ~1) | next(bit_iter)
            g = (g & ~1) | next(bit_iter)
            b = (b & ~1) | next(bit_iter)
        except StopIteration:
            new_pixels.append((r, g, b, px[3]) if has_alpha else (r, g, b))
            continue
        new_pixels.append((r, g, b, px[3]) if has_alpha else (r, g, b))

    if len(new_pixels) < len(pixels):
        new_pixels.extend(pixels[len(new_pixels):])

    out_img = Image.new(img.mode, img.size)
    out_img.putdata(new_pixels)
    return out_img

def extract_bytes_from_image_obj(img: Image.Image) -> bytes:
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")

    channel_lsbs = []
    for px in img.getdata():
        r, g, b = px[0], px[1], px[2]
        channel_lsbs.extend([r & 1, g & 1, b & 1])

    if len(channel_lsbs) < 32:
        raise ValueError("Header tidak ditemukan atau gambar terlalu kecil.")

    header_bits = channel_lsbs[:32]
    data_len = _bits_to_int(header_bits)
    total_data_bits = data_len * 8
    start_index = 32
    end_index = start_index + total_data_bits
    if end_index > len(channel_lsbs):
        raise ValueError("Payload tidak lengkap atau korup.")

    data_bits = channel_lsbs[start_index:end_index]
    return _bits_to_bytes(data_bits)

########## Routes ##########
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/embed", methods=["POST"])
def embed_route():
    try:
        cover_file = request.files.get("cover_image")
        if not cover_file or cover_file.filename == "":
            flash("Pilih cover image (PNG/BMP direkomendasikan).")
            return redirect(url_for("index"))

        cover_bytes = cover_file.read()
        try:
            cover_img = Image.open(BytesIO(cover_bytes))
        except Exception:
            flash("Gagal membuka image. Pastikan file image valid.")
            return redirect(url_for("index"))

        secret_bytes = b""
        secret_filename = "secret.txt"
        secret_upload = request.files.get("secret_file")
        if secret_upload and secret_upload.filename != "":
            secret_bytes = secret_upload.read()
            secret_filename = secret_upload.filename
        else:
            secret_text = request.form.get("secret_text", "").strip()
            if not secret_text:
                flash("Masukkan teks atau upload file rahasia.")
                return redirect(url_for("index"))
            secret_bytes = secret_text.encode("utf-8")

        fn_bytes = secret_filename.encode("utf-8")
        if len(fn_bytes) > 65535:
            flash("Nama file terlalu panjang.")
            return redirect(url_for("index"))
        payload = len(fn_bytes).to_bytes(2, "big") + fn_bytes + secret_bytes

        out_img = embed_bytes_into_image_obj(cover_img, payload)

        buf = BytesIO()
        out_img.save(buf, format="PNG")
        buf.seek(0)
        base = os.path.splitext(cover_file.filename)[0] or "stego"
        return send_file(buf, mimetype="image/png", as_attachment=True, download_name=f"stego_{base}.png")

    except ValueError as ve:
        flash(str(ve))
        return redirect(url_for("index"))
    except Exception as e:
        flash(f"Terjadi kesalahan saat embedding: {e}")
        return redirect(url_for("index"))

@app.route("/extract", methods=["POST"])
def extract_route():
    try:
        stego_file = request.files.get("stego_image")
        if not stego_file or stego_file.filename == "":
            flash("Pilih stego image.")
            return redirect(url_for("index"))

        stego_bytes = stego_file.read()
        try:
            stego_img = Image.open(BytesIO(stego_bytes))
        except Exception:
            flash("Gagal membuka image. Pastikan file image valid.")
            return redirect(url_for("index"))

        payload = extract_bytes_from_image_obj(stego_img)

        if len(payload) < 2:
            flash("Payload tidak valid.")
            return redirect(url_for("index"))

        fn_len = int.from_bytes(payload[:2], "big")
        if len(payload) < 2 + fn_len:
            flash("Payload korup / tidak lengkap.")
            return redirect(url_for("index"))

        filename = payload[2:2+fn_len].decode("utf-8", errors="replace")
        filedata = payload[2+fn_len:]

        session["extracted_filename"] = filename
        session["extracted_filedata"] = filedata

        # Preview
        is_text = False
        preview_text = ""
        try:
            preview_text = filedata.decode("utf-8")
            if len(preview_text) > 2000:
                preview_text = preview_text[:2000] + "\n...[dipotong]"
            is_text = True
        except Exception:
            pass

        return render_template("result.html", filename=filename, is_text=is_text, preview_text=preview_text)

    except ValueError as ve:
        flash(str(ve))
        return redirect(url_for("index"))
    except Exception as e:
        flash(f"Terjadi kesalahan saat ekstraksi: {e}")
        return redirect(url_for("index"))

@app.route("/download_extracted")
def download_extracted():
    if "extracted_filedata" not in session:
        flash("Tidak ada file diekstrak.")
        return redirect(url_for("index"))

    filedata = session["extracted_filedata"]
    filename = session.get("extracted_filename", "secret.bin")
    buf = BytesIO()
    buf.write(filedata)
    buf.seek(0)
    return send_file(buf, mimetype="application/octet-stream", as_attachment=True, download_name=filename)

if __name__ == "__main__":
    app.run(debug=True)
