from flask import Flask, render_template, request, send_file, flash, redirect, url_for
from PIL import Image
from io import BytesIO
import sys
import os

app = Flask(__name__)
app.secret_key = os.urandom(24)  # untuk flash messages; ganti di produksi

########## Helper LSB functions (adapted for in-memory files) ##########

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
    return w * h * 3  # using R,G,B LSB

def embed_bytes_into_image_obj(img: Image.Image, data: bytes) -> Image.Image:
    """Return new Image object with data embedded (header 32-bit length + bytes)."""
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")

    has_alpha = (img.mode == "RGBA")
    w, h = img.size
    pixels = list(img.getdata())

    total_capacity = capacity_in_bits(img)
    data_len = len(data)
    needed_bits = 32 + data_len * 8
    if needed_bits > total_capacity:
        raise ValueError(f"Not enough capacity. Need {needed_bits} bits, but image can hold {total_capacity} bits.")

    header_bits = _int_to_bits(data_len, 32)
    data_bits = _bytes_to_bits(data)
    bitstream = header_bits + data_bits
    bit_iter = iter(bitstream)

    new_pixels = []
    for px in pixels:
        # px tuple of length 3 or 4
        r, g, b = px[0], px[1], px[2]
        try:
            bit = next(bit_iter)
            r = (r & ~1) | bit
        except StopIteration:
            new_pixels.append((r, g, b, px[3]) if has_alpha else (r, g, b))
            continue

        try:
            bit = next(bit_iter)
            g = (g & ~1) | bit
        except StopIteration:
            new_pixels.append((r, g, b, px[3]) if has_alpha else (r, g, b))
            continue

        try:
            bit = next(bit_iter)
            b = (b & ~1) | bit
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
    """Extract and return embedded raw bytes."""
    if img.mode not in ("RGB", "RGBA"):
        img = img.convert("RGBA" if "A" in img.getbands() else "RGB")

    pixels = list(img.getdata())
    # build flat list of channel LSBs
    channel_lsbs = []
    for px in pixels:
        r, g, b = px[0], px[1], px[2]
        channel_lsbs.extend([r & 1, g & 1, b & 1])

    if len(channel_lsbs) < 32:
        raise ValueError("Image too small or no embedded header found.")

    header_bits = channel_lsbs[:32]
    data_len = _bits_to_int(header_bits)
    total_data_bits = data_len * 8

    start_index = 32
    end_index = start_index + total_data_bits
    if end_index > len(channel_lsbs):
        raise ValueError("Image does not contain full embedded data or is corrupted.")

    data_bits = channel_lsbs[start_index:end_index]
    data_bytes = _bits_to_bytes(data_bits)
    return data_bytes

########## Flask routes ##########
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATE_DIR = os.path.join(BASE_DIR, "templates")

app = Flask(__name__, template_folder=TEMPLATE_DIR)
@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")

@app.route("/embed", methods=["POST"])
def embed_route():
    # form fields:
    # - cover_image (file)
    # - secret_file (file) OR secret_text (text)
    # returns: image download
    try:
        if "cover_image" not in request.files:
            flash("Tidak ada file cover image diupload.")
            return redirect(url_for("index"))
        cover_file = request.files["cover_image"]
        if cover_file.filename == "":
            flash("Pilih file cover image.")
            return redirect(url_for("index"))

        # read image
        cover_bytes = cover_file.read()
        try:
            cover_img = Image.open(BytesIO(cover_bytes))
        except Exception as e:
            flash("Gagal membuka image. Pastikan file image valid (PNG/BMP/RGB).")
            return redirect(url_for("index"))

        # choose secret bytes from uploaded file or text input
        secret_bytes = b""
        if "secret_file" in request.files and request.files["secret_file"].filename != "":
            sf = request.files["secret_file"]
            secret_bytes = sf.read()
            secret_filename = sf.filename
        else:
            secret_text = request.form.get("secret_text", "")
            if secret_text.strip() == "":
                flash("Berikan secret: upload file atau masukkan teks.")
                return redirect(url_for("index"))
            secret_bytes = secret_text.encode("utf-8")
            secret_filename = "secret.txt"

        # optional label: we will store the filename as first part of payload (length + name + marker + data)
        # To keep simple, we'll prefix the payload with the filename length (16-bit) + filename bytes + raw data
        fn_bytes = secret_filename.encode("utf-8")
        if len(fn_bytes) > 65535:
            flash("Nama file terlalu panjang.")
            return redirect(url_for("index"))

        fn_len_bits = len(fn_bytes).to_bytes(2, "big")  # 2 bytes for filename length
        payload = fn_len_bits + fn_bytes + secret_bytes

        # attempt embedding
        try:
            out_img = embed_bytes_into_image_obj(cover_img, payload)
        except ValueError as ve:
            flash(str(ve))
            return redirect(url_for("index"))

        # prepare image bytes to send
        buf = BytesIO()
        # preserve PNG to avoid lossy compression
        out_format = "PNG"
        out_img.save(buf, format=out_format)
        buf.seek(0)

        return send_file(
            buf,
            mimetype="image/png",
            as_attachment=True,
            download_name=f"stego_{cover_file.filename.rsplit('.',1)[0]}.png"
        )

    except Exception as e:
        flash("Terjadi kesalahan saat embedding: " + str(e))
        return redirect(url_for("index"))

@app.route("/extract", methods=["POST"])
def extract_route():
    try:
        if "stego_image" not in request.files:
            flash("Tidak ada file image untuk diekstrak.")
            return redirect(url_for("index"))
        stego_file = request.files["stego_image"]
        if stego_file.filename == "":
            flash("Pilih file stego image.")
            return redirect(url_for("index"))

        stego_bytes = stego_file.read()
        try:
            stego_img = Image.open(BytesIO(stego_bytes))
        except Exception:
            flash("Gagal membuka image. Pastikan file image valid.")
            return redirect(url_for("index"))

        try:
            payload = extract_bytes_from_image_obj(stego_img)
        except ValueError as ve:
            flash(str(ve))
            return redirect(url_for("index"))

        # parse payload: first 2 bytes = filename length, then filename, then data
        if len(payload) < 2:
            flash("Payload tidak valid / terlalu pendek.")
            return redirect(url_for("index"))

        fn_len = int.from_bytes(payload[:2], "big")
        if len(payload) < 2 + fn_len:
            flash("Payload korup atau tidak lengkap.")
            return redirect(url_for("index"))

        filename = payload[2:2+fn_len].decode("utf-8", errors="replace")
        filedata = payload[2+fn_len:]

        # if filedata looks like text (utf-8 decodeable), we can show a small preview.
        is_text = False
        preview_text = ""
        try:
            preview_text = filedata.decode("utf-8")
            # limit preview length
            if len(preview_text) > 1000:
                preview_text = preview_text[:1000] + "\n...[truncated]"
            is_text = True
        except Exception:
            is_text = False

        # prepare file to download
        buf = BytesIO()
        buf.write(filedata)
        buf.seek(0)

        return send_file(
            buf,
            mimetype="application/octet-stream",
            as_attachment=True,
            download_name=filename
        )

    except Exception as e:
        flash("Terjadi kesalahan saat ekstraksi: " + str(e))
        return redirect(url_for("index"))

if __name__ == "__main__":
    app.run(debug=True, port=8000)
