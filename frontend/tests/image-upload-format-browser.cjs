// Browser-side raster conversion used by clipboard and selected-file uploads.
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || "playwright");
const { spawn } = require("node:child_process");
const path = require("node:path");
const assert = require("node:assert/strict");

const root = path.resolve(__dirname, "../..");
const port = 15184;
const base = `http://127.0.0.1:${port}`;
const server = spawn(process.execPath, ["frontend/node_modules/vite/bin/vite.js", "frontend", "--host", "127.0.0.1", "--port", String(port), "--strictPort"], { cwd: root, stdio: "ignore" });

(async () => {
  let browser;
  try {
    for (let attempt = 0; attempt < 100; attempt += 1) {
      try { if ((await fetch(base)).ok) break; } catch {}
      await new Promise(resolve => setTimeout(resolve, 100));
    }
    browser = await chromium.launch({ headless: true, executablePath: process.env.CHROME_PATH || "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" });
    const page = await browser.newPage();
    await page.goto(base);
    const result = await page.evaluate(async () => {
      const { prepareImageUpload } = await import("/src/imageUpload.js");
      const encode = (canvas, type, quality) => new Promise(resolve => canvas.toBlob(resolve, type, quality));
      const source = document.createElement("canvas");
      source.width = 4;
      source.height = 4;
      const context = source.getContext("2d");
      context.clearRect(0, 0, 4, 4);
      context.fillStyle = "rgba(255, 0, 0, 0.5)";
      context.fillRect(0, 0, 4, 4);
      const png = await encode(source, "image/png");
      const pngFile = new File([png], "clipboard-image.png", { type: "image/png", lastModified: 123 });
      const converted = await prepareImageUpload(pngFile);
      const convertedBytes = new Uint8Array(await converted.arrayBuffer());
      const decoded = await createImageBitmap(converted);
      const check = document.createElement("canvas");
      check.width = decoded.width;
      check.height = decoded.height;
      const pixelContext = check.getContext("2d", { willReadFrequently: true });
      pixelContext.drawImage(decoded, 0, 0);
      const decodedDimensions = [decoded.width, decoded.height];
      const pixel = [...pixelContext.getImageData(2, 2, 1, 1).data];
      decoded.close();

      const mislabeled = await prepareImageUpload(new File([png], "wrong.gif", { type: "image/gif" }));
      const webp = await encode(source, "image/webp", 0.88);
      const webpFile = new File([webp], "clipboard.webp", { type: "image/webp" });
      const normalizedWebp = await prepareImageUpload(webpFile);

      const jpegBlob = await encode(source, "image/jpeg", 0.88);
      const jpegFile = new File([jpegBlob], "cover.PNG", { type: "image/jpeg", lastModified: 456 });
      const normalizedJpeg = await prepareImageUpload(jpegFile);

      const gifBytes = new Uint8Array([0x47, 0x49, 0x46, 0x38, 0x39, 0x61, 1, 0, 1, 0, 0x80, 0, 0, 0, 0, 0, 0xff, 0xff, 0xff, 0x2c, 0, 0, 0, 0, 1, 0, 1, 0, 0, 2, 2, 0x44, 1, 0, 0x3b]);
      const gif = await prepareImageUpload(new File([gifBytes], "animation.GIF", { type: "image/gif" }));
      const gifOut = new Uint8Array(await gif.arrayBuffer());
      let oversizedMessage = "";
      try { await prepareImageUpload(new File([new Uint8Array(20 * 1024 * 1024 + 1)], "too-large.png", { type: "image/png" })); }
      catch (error) { oversizedMessage = error.message; }
      return {
        convertedType: converted.type, convertedName: converted.name, convertedSize: converted.size,
        convertedSignature: [...convertedBytes.slice(0, 3)], convertedDimensions: decodedDimensions, pixel,
        mislabeledType: mislabeled.type, mislabeledName: mislabeled.name,
        webpType: normalizedWebp.type, webpName: normalizedWebp.name,
        jpegType: normalizedJpeg.type, jpegName: normalizedJpeg.name,
        gifType: gif.type, gifName: gif.name, gifPreservedByteForByte: JSON.stringify([...gifOut]) === JSON.stringify([...gifBytes]),
        oversizedMessage,
      };
    });
    assert.equal(result.convertedType, "image/jpeg");
    assert.equal(result.convertedName, "clipboard-image.jpg");
    assert.deepEqual(result.convertedSignature, [0xff, 0xd8, 0xff]);
    assert.deepEqual(result.convertedDimensions, [4, 4]);
    assert.ok(result.convertedSize > 0 && result.convertedSize <= 20 * 1024 * 1024);
    assert.ok(result.pixel[0] > 220 && result.pixel[1] > 75 && result.pixel[2] > 75, `transparency matte was not white: ${result.pixel}`);
    assert.equal(result.mislabeledType, "image/jpeg");
    assert.equal(result.mislabeledName, "wrong.jpg");
    assert.equal(result.webpType, "image/jpeg");
    assert.equal(result.webpName, "clipboard.jpg");
    assert.equal(result.jpegType, "image/jpeg");
    assert.equal(result.jpegName, "cover.jpg");
    assert.equal(result.gifType, "image/gif");
    assert.equal(result.gifName, "animation.gif");
    assert.equal(result.gifPreservedByteForByte, true);
    assert.match(result.oversizedMessage, /20 MB/);
    console.log("PASS: PNG/WebP and mislabeled non-GIF bytes convert to JPEG; JPEG filename/MIME normalize; GIF bytes stay intact; size bound is enforced.");
  } finally {
    await browser?.close();
    server.kill("SIGTERM");
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
