import { t } from "./i18n";

export const IMAGE_UPLOAD_MAX_BYTES = 20 * 1024 * 1024;
const JPEG_QUALITY_STEPS = [0.9, 0.82, 0.74, 0.66];

function hasSignature(bytes, signature) {
  return signature.every((value, index) => bytes[index] === value);
}

function detectedImageType(bytes) {
  if (bytes[0] === 0x47 && bytes[1] === 0x49 && bytes[2] === 0x46
    && bytes[3] === 0x38 && (bytes[4] === 0x37 || bytes[4] === 0x39) && bytes[5] === 0x61) return "image/gif";
  if (hasSignature(bytes, [0xff, 0xd8, 0xff])) return "image/jpeg";
  if (hasSignature(bytes, [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) return "image/png";
  if (hasSignature(bytes, [0x42, 0x4d])) return "image/bmp";
  if (hasSignature(bytes, [0x52, 0x49, 0x46, 0x46]) && hasSignature(bytes.slice(8, 12), [0x57, 0x45, 0x42, 0x50])) return "image/webp";
  if (hasSignature(bytes.slice(4, 8), [0x66, 0x74, 0x79, 0x70])
    && (hasSignature(bytes.slice(8, 12), [0x61, 0x76, 0x69, 0x66]) || hasSignature(bytes.slice(8, 12), [0x61, 0x76, 0x69, 0x73]))) return "image/avif";
  return "";
}

function imageBaseName(name, fallback) {
  const value = String(name || fallback).split(/[\\/]/).pop().replace(/\.[^.]*$/, "").replace(/[\u0000-\u001f\u007f]/g, "").trim();
  return value || fallback;
}

function asFile(file, extension, contentType) {
  const fallback = contentType === "image/gif" ? "clipboard-image" : "image";
  const name = `${imageBaseName(file.name, fallback)}.${extension}`;
  if (file.name === name && file.type === contentType) return file;
  return new File([file], name, { type: contentType, lastModified: file.lastModified || Date.now() });
}

function canvasBlob(canvas, quality) {
  return new Promise((resolve, reject) => {
    try {
      canvas.toBlob(blob => blob ? resolve(blob) : reject(new Error(t("图片无法转换为 JPEG"))), "image/jpeg", quality);
    } catch (error) {
      reject(error);
    }
  });
}

/** Keep GIF bytes/animation; normalize all supported raster uploads to a .jpg JPEG file. */
export async function prepareImageUpload(file) {
  if (!file || typeof file.slice !== "function" || typeof file.size !== "number") throw new Error(t("图片文件无效"));
  if (file.size > IMAGE_UPLOAD_MAX_BYTES) throw new Error(t("单张图片不能超过 20 MB"));
  const header = new Uint8Array(await file.slice(0, 12).arrayBuffer());
  const detectedType = detectedImageType(header);
  if (detectedType === "image/gif") return asFile(file, "gif", "image/gif");
  if (detectedType === "image/jpeg") return asFile(file, "jpg", "image/jpeg");

  const supportedTypes = new Set(["image/png", "image/webp", "image/bmp", "image/avif"]);
  const sourceType = detectedType || String(file.type || "").toLowerCase();
  if (!supportedTypes.has(sourceType)) throw new Error(t("只支持 JPEG、PNG、GIF、WebP、BMP 或 AVIF 图片"));
  if (typeof globalThis.createImageBitmap !== "function") throw new Error(t("当前浏览器无法将图片转换为 JPEG"));

  let bitmap;
  try {
    const source = file.type === sourceType ? file : new Blob([file], { type: sourceType });
    bitmap = await createImageBitmap(source);
    if (!bitmap.width || !bitmap.height || bitmap.width > 10000 || bitmap.height > 10000) {
      throw new Error(t("图片尺寸无效或超过 10000 像素"));
    }
    const canvas = document.createElement("canvas");
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const context = canvas.getContext("2d", { alpha: false });
    if (!context) throw new Error(t("当前浏览器无法转换图片"));
    // A white matte makes transparent PNG/WebP pixels deterministic in JPEG.
    context.fillStyle = "#fff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.drawImage(bitmap, 0, 0);
    let blob;
    for (const quality of JPEG_QUALITY_STEPS) {
      blob = await canvasBlob(canvas, quality);
      if (blob.size <= IMAGE_UPLOAD_MAX_BYTES) break;
    }
    if (!blob || blob.size > IMAGE_UPLOAD_MAX_BYTES) throw new Error(t("转换后的 JPG 仍超过 20 MB"));
    const name = `${imageBaseName(file.name, "image")}.jpg`;
    return new File([blob], name, { type: "image/jpeg", lastModified: file.lastModified || Date.now() });
  } catch (error) {
    if (error instanceof Error && /^(图片|只支持|当前浏览器|转换后的)/.test(error.message)) throw error;
    throw new Error(t("图片无法解码或转换为 JPEG"));
  } finally {
    bitmap?.close?.();
  }
}
