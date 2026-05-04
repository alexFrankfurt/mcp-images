#!/usr/bin/env python3

import os
import sys
import asyncio
import httpx
import logging
from io import BytesIO
import base64
from datetime import datetime
from PIL import Image as PILImage
from urllib.parse import urlparse
from mcp.server.fastmcp import FastMCP, Image, Context
from typing import List, Dict, Any, Union, Optional

MAX_IMAGE_SIZE = 1024  # Maximum dimension size in pixels
TEMP_DIR = "./Temp"
DATA_DIR = "./data"

# Ensure directories exist
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

# Configure logging: first disable other loggers
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)

# Configure our logger
log_filename = os.path.join(DATA_DIR, datetime.now().strftime("%d-%m-%y.log"))
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

# Create handlers
file_handler = logging.FileHandler(log_filename)
file_handler.setFormatter(formatter)
console_handler = logging.StreamHandler(sys.stderr)
console_handler.setFormatter(formatter)

# Set up our logger
logger = logging.getLogger("image-mcp")
logger.setLevel(logging.DEBUG)
logger.addHandler(file_handler)
logger.addHandler(console_handler)
# Prevent double logging
logger.propagate = False

# Create a FastMCP server instance
mcp = FastMCP("image-service")

async def process_image_data(data: bytes, content_type: str, image_source: str, ctx: Context):
    """Process image data and return an MCP Image object."""
    try:
        # If image is not large, try to log dimensions without processing
        if len(data) <= 1048576:
            try:
                with PILImage.open(BytesIO(data)) as img:
                    width, height = img.size
                    logger.debug(f"Original image dimensions from {image_source}: {width}x{height}")
                    logger.debug(f"Image format from PIL: {img.format}, mode: {img.mode}")
            except Exception as e:
                logger.debug(f"Could not determine dimensions for {image_source}: {e}")

            # Ensure content_type is valid and doesn't include 'image/'
            if content_type.startswith('image/'):
                content_type = content_type.split('/')[-1]

            logger.debug(f"Creating Image with format: {content_type}")
            return Image(data=data, format=content_type)

        # For large images, save to temp file and process
        temp_path = os.path.join(TEMP_DIR, f"temp_image_{hash(image_source)}." + content_type.split('/')[-1])
        with open(temp_path, "wb") as f:
            f.write(data)

        try:
            # First pass: get dimensions and basic info
            with PILImage.open(temp_path) as img:
                orig_width, orig_height = img.size
                orig_format = img.format
                orig_mode = img.mode
                logger.debug(f"Original image dimensions from {image_source}: {orig_width}x{orig_height}")
                logger.debug(f"Large image format from PIL: {orig_format}, mode: {orig_mode}")

            # Calculate optimal resize factor if image is very large
            max_dimension = max(orig_width, orig_height)
            initial_scale = 1.0
            if max_dimension > 3000:
                initial_scale = 3000 / max_dimension
                logger.debug(f"Very large image detected ({max_dimension}px), will start with scale factor: {initial_scale}")

            # Second pass: process the image
            with PILImage.open(temp_path) as img:
                if img.mode in ('RGBA', 'P'):
                    img = img.convert('RGB')

                # Apply initial scale if needed
                if initial_scale < 1.0:
                    width = int(orig_width * initial_scale)
                    height = int(orig_height * initial_scale)
                    img = img.resize((width, height), PILImage.LANCZOS)
                else:
                    width, height = img.size

                quality = 85
                scale_factor = 1.0

                while True:
                    img_byte_arr = BytesIO()

                    # Create a copy for this iteration to avoid accumulating transforms
                    if scale_factor < 1.0:
                        current_width = int(width * scale_factor)
                        current_height = int(height * scale_factor)
                        current_img = img.resize((current_width, current_height), PILImage.LANCZOS)
                    else:
                        current_img = img
                        current_width, current_height = width, height

                    current_img.save(img_byte_arr, format='JPEG', quality=quality, optimize=True)
                    processed_data = img_byte_arr.getvalue()

                    # Clean up the temporary image if we created one
                    if scale_factor < 1.0 and hasattr(current_img, 'close'):
                        current_img.close()

                    # Target 800KB to leave buffer for any MCP overhead
                    if len(processed_data) <= 819200:  # 800KB
                        logger.debug(f"Processed image dimensions from {image_source}: {current_width}x{current_height} (quality={quality})")
                        logger.debug(f"Returning processed image with format: jpeg, size: {len(processed_data)} bytes")
                        return Image(data=processed_data, format="jpeg")

                    # Try reducing quality first
                    if quality > 20:
                        quality -= 10
                        logger.debug(f"Reducing quality to {quality} for {image_source}, current size: {len(processed_data)} bytes")
                    else:
                        # Then try scaling down
                        scale_factor *= 0.8
                        if current_width * scale_factor < 200 or current_height * scale_factor < 200:
                            ctx.error("Unable to compress image to acceptable size while maintaining quality")
                            logger.error(f"Failed processing image from {image_source}: dimensions too small")
                            return None
                        logger.debug(f"Applying scale factor {scale_factor} to image from {image_source}")
                        quality = 85  # Reset quality when changing size
        except MemoryError as e:
            ctx.error(f"Out of memory processing large image: {str(e)}")
            logger.error(f"MemoryError processing image from {image_source}: {str(e)}")
            return None
        except Exception as e:
            ctx.error(f"Image processing error: {str(e)}")
            logger.exception(f"Exception processing image from {image_source}")
            return None
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    except Exception as e:
        ctx.error(f"Error processing image: {str(e)}")
        logger.exception(f"Unexpected error processing {image_source}")
        return None

async def process_local_image(file_path: str, ctx: Context) -> Dict[str, Any]:
    """Processes a local image file and returns a dictionary with the result."""
    try:
        if not os.path.exists(file_path):
            error_msg = f"File not found: {file_path}"
            ctx.error(error_msg)
            logger.error(error_msg)
            return {"path": file_path, "error": error_msg}

        # Determine content type based on file extension
        _, ext = os.path.splitext(file_path)
        ext = ext[1:].lower() if ext else "jpeg"  # Default to jpeg if no extension

        # Map extension to proper MIME type
        mime_type_map = {
            "jpg": "jpeg",
            "jpeg": "jpeg",
            "png": "png",
            "gif": "gif",
            "bmp": "bmp",
            "webp": "webp",
            "tiff": "tiff",
            "tif": "tiff"
        }

        content_type = mime_type_map.get(ext, "jpeg")  # Default to jpeg if unknown extension
        logger.debug(f"Local image {file_path} has extension '{ext}', mapped to content type '{content_type}'")

        # For large files, read and process directly without loading entire file into memory
        file_size = os.path.getsize(file_path)
        if file_size > 1048576:
            logger.debug(f"Large local image detected: {file_path} ({file_size} bytes)")
            # Process the image directly using the same logic as for URL images
            return await process_large_local_image(file_path, content_type, ctx)

        # For smaller files, read the entire content
        with open(file_path, "rb") as f:
            file_data = f.read()

        logger.debug(f"Read local image from {file_path} with {len(file_data)} bytes")
        processed_image = await process_image_data(file_data, content_type, file_path, ctx)

        if processed_image is None:
            return {"path": file_path, "error": "Failed to process image"}

        return {"path": file_path, "image": processed_image}
        
    except Exception as e:
        error_msg = f"Error processing local image {file_path}: {str(e)}"
        ctx.error(error_msg)
        logger.exception(error_msg)
        return {"path": file_path, "error": error_msg}

async def process_large_local_image(file_path: str, content_type: str, ctx: Context) -> Dict[str, Any]:
    """Process a large local image file directly without loading it entirely into memory."""
    temp_path = None
    try:
        # First pass: get dimensions and basic info
        with PILImage.open(file_path) as img:
            orig_width, orig_height = img.size
            orig_format = img.format
            orig_mode = img.mode
            logger.debug(f"Original large local image dimensions from {file_path}: {orig_width}x{orig_height}")
            logger.debug(f"Original image format: {orig_format}, mode: {orig_mode}")

        # Calculate optimal resize factor if image is very large
        max_dimension = max(orig_width, orig_height)
        initial_scale = 1.0
        if max_dimension > 4000:
            initial_scale = 4000 / max_dimension
            logger.debug(f"Very large image detected, will start with scale factor: {initial_scale}")

        # Second pass: process the image
        with PILImage.open(file_path) as img:
            if img.mode in ('RGBA', 'P'):
                img = img.convert('RGB')

            # Apply initial scale if needed
            if initial_scale < 1.0:
                width = int(orig_width * initial_scale)
                height = int(orig_height * initial_scale)
                img = img.resize((width, height), PILImage.LANCZOS)
            else:
                width, height = img.size

            quality = 75  # Start with lower quality for large images
            scale_factor = 1.0

            while True:
                # Save the processed image to a temporary BytesIO
                img_byte_arr = BytesIO()

                # Create a copy for this iteration to avoid accumulating transforms
                if scale_factor < 1.0:
                    current_width = int(width * scale_factor)
                    current_height = int(height * scale_factor)
                    current_img = img.resize((current_width, current_height), PILImage.LANCZOS)
                else:
                    current_img = img
                    current_width, current_height = width, height

                current_img.save(img_byte_arr, format='JPEG', quality=quality, optimize=True)
                processed_data = img_byte_arr.getvalue()

                # Clean up the temporary image if we created one
                if scale_factor < 1.0 and hasattr(current_img, 'close'):
                    current_img.close()

                # Target 800KB to leave buffer for any MCP overhead
                if len(processed_data) <= 819200:  # 800KB
                    logger.debug(f"Successfully compressed large local image {file_path} to {len(processed_data)} bytes (quality={quality}, dimensions={current_width}x{current_height})")
                    return {
                        "path": file_path,
                        "image": Image(data=processed_data, format="jpeg")
                    }

                # Try reducing quality first
                if quality > 30:
                    quality -= 10
                    logger.debug(f"Reducing quality to {quality} for {file_path}")
                else:
                    # Then try scaling down
                    scale_factor *= 0.8
                    if current_width * scale_factor < 200 or current_height * scale_factor < 200:
                        error_msg = f"Unable to compress large local image {file_path} to acceptable size while maintaining quality"
                        ctx.error(error_msg)
                        logger.error(error_msg)
                        return {"path": file_path, "error": error_msg}

                    logger.debug(f"Applying scale factor {scale_factor} to image {file_path}")
                    quality = 85  # Reset quality when changing size

    except MemoryError as e:
        error_msg = f"Out of memory processing large local image {file_path}: {str(e)}"
        ctx.error(error_msg)
        logger.error(error_msg)
        return {"path": file_path, "error": error_msg}
    except Exception as e:
        error_msg = f"Error processing large local image {file_path}: {str(e)}"
        ctx.error(error_msg)
        logger.exception(error_msg)
        return {"path": file_path, "error": error_msg}

async def fetch_single_image(url: str, client: httpx.AsyncClient, ctx: Context) -> Dict[str, Any]:
    """Fetches and processes a single image asynchronously."""
    try:
        parsed = urlparse(url)
        if not all([parsed.scheme in ['http', 'https'], parsed.netloc]):
            error_msg = f"Invalid URL: {url}"
            ctx.error(error_msg)
            return {"url": url, "error": error_msg}

        response = await client.get(url)
        response.raise_for_status()

        content_type = response.headers.get('content-type', '')
        if not content_type.startswith('image/'):
            error_msg = f"Not an image (got {content_type})"
            ctx.error(error_msg)
            return {"url": url, "error": error_msg}

        logger.debug(f"Fetched image from {url} with {len(response.content)} bytes")
        logger.debug(f"Content-Type from server: {content_type}")

        # Extract the format from content-type
        format_type = content_type.split('/')[-1]
        logger.debug(f"Extracted format type: {format_type}")

        processed_image = await process_image_data(response.content, format_type, url, ctx)

        if processed_image is None:
            return {"url": url, "error": "Failed to process image"}

        return {"url": url, "image": processed_image}

    except httpx.HTTPError as e:
        error_msg = f"HTTP error: {str(e)}"
        ctx.error(error_msg)
        return {"url": url, "error": error_msg}
    except Exception as e:
        error_msg = f"Unexpected error: {str(e)}"
        ctx.error(error_msg)
        return {"url": url, "error": error_msg}

def is_url(path_or_url: str) -> bool:
    """Determine if the given string is a URL or a local file path."""
    parsed = urlparse(path_or_url)
    return bool(parsed.scheme and parsed.netloc)


async def format_image_output(image_data: bytes, image_format: str, output_mode: str, output_path: str,
                              ctx: Context, description: str = "") -> Dict[str, Any]:
    """Format image output based on output mode - stream or file."""
    if output_mode == "file":
        if not output_path:
            error_msg = "output_path is required when output_mode is 'file'"
            if ctx is not None:
                ctx.error(error_msg)
            logger.error(error_msg)
            return {"error": error_msg}

        try:
            # Ensure directory exists
            os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

            with open(output_path, "wb") as f:
                f.write(image_data)

            logger.debug(f"Saved image to {output_path}")
            return {
                "text": f"{description} Saved to: {output_path}",
                "file_path": output_path
            }
        except Exception as e:
            error_msg = f"Failed to save image to {output_path}: {str(e)}"
            if ctx is not None:
                ctx.error(error_msg)
            logger.error(error_msg)
            return {"error": error_msg}

    # Default: stream mode
    return {"image": Image(data=image_data, format=image_format)}


def is_image_file_path(path: str) -> bool:
    """Check if path looks like a file path with an image extension."""
    ext = os.path.splitext(path)[1].lower()
    return ext in ('.png', '.jpg', '.jpeg', '.gif', '.bmp', '.webp', '.tiff', '.tif')


async def process_images_async(image_sources: List[str], ctx: Context) -> List[Dict[str, Any]]:
    """Process multiple images (URLs or local files) concurrently."""
    if not image_sources:
        raise ValueError("No image sources provided")
    
    # Separate URLs from local file paths
    urls = [src for src in image_sources if is_url(src)]
    local_paths = [src for src in image_sources if not is_url(src)]
    
    results = []
    
    # Process URLs if any
    if urls:
        logger.debug(f"Processing {len(urls)} URLs")
        async with httpx.AsyncClient() as client:
            url_tasks = [fetch_single_image(url, client, ctx) for url in urls]
            url_results = await asyncio.gather(*url_tasks)
            results.extend(url_results)
    
    # Process local files if any
    if local_paths:
        logger.debug(f"Processing {len(local_paths)} local files")
        local_tasks = [process_local_image(path, ctx) for path in local_paths]
        local_results = await asyncio.gather(*local_tasks)
        results.extend(local_results)
    
    # Ensure results are in the same order as input sources
    ordered_results = []
    for src in image_sources:
        for result in results:
            if (src == result.get("url", None)) or (src == result.get("path", None)):
                ordered_results.append(result)
                break
    
    return ordered_results

@mcp.tool()
async def fetch_images(image_sources: List[str], output_mode: str = "stream", output_path: str = None, ctx: Context = None):
    """
    Fetch and process images from URLs or local file paths, returning them in a format suitable for LLMs.

    This tool accepts a list of image sources which can be either:
    1. URLs pointing to web-hosted images (http:// or https://)
    2. Local file paths pointing to images stored on the local filesystem (e.g., "C:/images/photo1.jpg")

    For a single image, provide a one-element list. The function will process images in parallel
    when multiple sources are provided. Images that exceed the size limit (1MB) will be automatically
    compressed while maintaining aspect ratio and reasonable quality.

    Args:
        image_sources: A list of image URLs or local file paths. For a single image, provide a one-element list.
        output_mode: Output destination - "stream" returns image in response (default), "file" writes to output_path
        output_path: File path or directory path for file output mode. For multiple images, this should be a directory.
                     For a single image, can be a specific file path (e.g., "C:/images/photo.png").

    Returns:
        A list containing text information about the processed images followed by Image objects (stream mode),
        or file paths where images were saved (file mode).
        Failed images will have an error message instead of an Image object.
    """
    try:
        start_time = asyncio.get_event_loop().time()

        # Validate input
        if not image_sources:
            if ctx is not None:
                ctx.error("No image sources provided")
            logger.error("fetch_images called with empty source list")
            return ["No image sources provided"]

        # Log the types of sources we're processing
        url_count = sum(1 for src in image_sources if is_url(src))
        local_count = len(image_sources) - url_count
        logger.debug(f"Processing {len(image_sources)} image sources: {url_count} URLs and {local_count} local files")

        # Process all images
        results = await process_images_async(image_sources, ctx)

        # Build response list with text + images (same pattern as snapshot tool)
        response_parts = []
        success_count = 0

        for i, (src, result) in enumerate(zip(image_sources, results)):
            if "image" in result:
                img_field = result["image"]
                # Handle both single Image and list of Images
                if isinstance(img_field, list):
                    if output_mode == "file":
                        saved_files = []
                        for j, img in enumerate(img_field):
                            ext = img.format if hasattr(img, 'format') else 'png'
                            # For single image source with a file-like output_path, use it directly
                            if len(image_sources) == 1 and output_path and is_image_file_path(output_path):
                                file_path = output_path
                            else:
                                file_path = os.path.join(output_path, f"image_{i+1}_{j+1}.{ext}") if output_path else None

                            output_result = await format_image_output(
                                img.data, ext, output_mode, file_path, ctx,
                                f"Successfully processed image {j+1} from: {src}"
                            )
                            if "file_path" in output_result:
                                saved_files.append(output_result["file_path"])
                            elif "error" in output_result:
                                response_parts.append(f"Failed to save image {j+1} from {src}: {output_result['error']}")

                        if saved_files:
                            response_parts.append(f"Successfully processed {len(img_field)} image(s) from: {src}")
                            response_parts.extend(saved_files)
                            success_count += 1
                    else:
                        response_parts.append(f"Successfully processed {len(img_field)} image(s) from: {src}")
                        for img in img_field:
                            response_parts.append(img)
                        success_count += 1
                else:
                    ext = img_field.format if hasattr(img_field, 'format') else 'png'
                    # For single image source with a file-like output_path, use it directly
                    if output_mode == "file" and len(image_sources) == 1 and output_path and is_image_file_path(output_path):
                        file_path = output_path
                    elif output_mode == "file" and output_path:
                        file_path = os.path.join(output_path, f"image_{i+1}.{ext}")
                    else:
                        file_path = output_path

                    output_result = await format_image_output(
                        img_field.data, ext, output_mode, file_path, ctx,
                        f"Successfully processed image from: {src}"
                    )

                    if "image" in output_result:
                        response_parts.append(f"Successfully processed image from: {src}")
                        response_parts.append(output_result["image"])
                        success_count += 1
                    elif "text" in output_result:
                        response_parts.append(output_result["text"])
                        success_count += 1
                    elif "error" in output_result:
                        response_parts.append(f"Failed to process {src}: {output_result['error']}")
            else:
                error_msg = result.get("error", "Unknown error")
                response_parts.append(f"Failed to process {src}: {error_msg}")

        elapsed = asyncio.get_event_loop().time() - start_time
        logger.debug(
            f"Processed {len(image_sources)} images in {elapsed:.2f} seconds. "
            f"Success: {success_count}, Failed: {len(image_sources) - success_count}"
        )

        return response_parts
    except Exception as e:
        logger.exception("Error in fetch_images")
        if ctx is not None:
            ctx.error(f"Failed to process images: {str(e)}")
        return [f"Error processing images: {str(e)}"]




# Cross-platform screen capture functionality
def get_platform_screenshot():
    """Capture screenshot using platform-appropriate method."""
    try:
        # Try to use PIL/Pillow's ImageGrab first (works on Windows, macOS, some Linux)
        from PIL import ImageGrab
        return ImageGrab.grab()
    except ImportError:
        # If PIL ImageGrab is not available, try mss (works cross-platform)
        try:
            import mss
            with mss.mss() as sct:
                # Get the primary monitor
                monitor = sct.monitors[1]  # monitors[0] is all monitors combined, monitors[1+] are individual
                screenshot = sct.grab(monitor)
                # Convert mss image to PIL Image
                return PILImage.frombytes("RGB", screenshot.size, screenshot.bgra, "raw", "BGRX")
        except ImportError:
            # If neither PIL ImageGrab nor mss are available, try pyautogui
            try:
                import pyautogui
                return pyautogui.screenshot()
            except ImportError:
                raise RuntimeError("No screen capture library available. Install pillow, mss, or pyautogui.")


def get_window_list():
    """Get a list of visible windows with their titles and positions.
    
    Returns a list of dictionaries with keys: title, left, top, right, bottom, width, height.
    Currently supports Windows via win32gui or ctypes.
    """
    import platform
    system = platform.system()
    windows = []
    
    if system == "Windows":
        try:
            import win32gui
            
            def enum_callback(hwnd, results):
                if win32gui.IsWindowVisible(hwnd):
                    title = win32gui.GetWindowText(hwnd)
                    if title:
                        rect = win32gui.GetWindowRect(hwnd)
                        left, top, right, bottom = rect
                        windows.append({
                            "title": title,
                            "left": left,
                            "top": top,
                            "right": right,
                            "bottom": bottom,
                            "width": right - left,
                            "height": bottom - top
                        })
            
            win32gui.EnumWindows(enum_callback, None)
            return windows
        except ImportError:
            # Fallback to ctypes
            import ctypes
            from ctypes import wintypes
            
            EnumWindows = ctypes.windll.user32.EnumWindows
            EnumWindowsProc = ctypes.WINFUNCTYPE(
                ctypes.c_bool, wintypes.HWND, ctypes.POINTER(ctypes.c_int)
            )
            GetWindowText = ctypes.windll.user32.GetWindowTextW
            GetWindowTextLength = ctypes.windll.user32.GetWindowTextLengthW
            IsWindowVisible = ctypes.windll.user32.IsWindowVisible
            GetWindowRect = ctypes.windll.user32.GetWindowRect
            
            def enum_callback(hwnd, _):
                if IsWindowVisible(hwnd):
                    length = GetWindowTextLength(hwnd)
                    if length > 0:
                        buffer = ctypes.create_unicode_buffer(length + 1)
                        GetWindowText(hwnd, buffer, length + 1)
                        title = buffer.value
                        rect = ctypes.wintypes.RECT()
                        GetWindowRect(hwnd, ctypes.byref(rect))
                        windows.append({
                            "title": title,
                            "left": rect.left,
                            "top": rect.top,
                            "right": rect.right,
                            "bottom": rect.bottom,
                            "width": rect.right - rect.left,
                            "height": rect.bottom - rect.top
                        })
                return True
            
            EnumWindows(EnumWindowsProc(enum_callback), 0)
            return windows
    else:
        # macOS and Linux: not currently supported for window enumeration
        return []


def capture_window_by_title(window_title: str):
    """Capture a screenshot of a specific window by its title.
    
    Args:
        window_title: The title of the window to capture. Supports partial matching.
        
    Returns:
        A PIL Image of the captured window region.
        
    Raises:
        RuntimeError: If the platform is not supported or window is not found.
    """
    import platform
    system = platform.system()
    
    if system != "Windows":
        raise RuntimeError(f"Window-specific capture is not supported on {system}. Only Windows is currently supported.")
    
    windows = get_window_list()
    if not windows:
        raise RuntimeError("No visible windows found.")
    
    # Try exact match first
    matched_window = None
    for win in windows:
        if win["title"] == window_title:
            matched_window = win
            break
    
    # Fall back to partial (case-insensitive) match
    if matched_window is None:
        search_lower = window_title.lower()
        for win in windows:
            if search_lower in win["title"].lower():
                matched_window = win
                break
    
    if matched_window is None:
        available = [w["title"] for w in windows if w["title"]]
        raise RuntimeError(f"Window with title '{window_title}' not found. Available windows: {available}")
    
    # Capture the window region
    from PIL import ImageGrab
    bbox = (
        matched_window["left"],
        matched_window["top"],
        matched_window["right"],
        matched_window["bottom"]
    )
    screenshot = ImageGrab.grab(bbox=bbox)
    return screenshot, matched_window["title"]

@mcp.tool()
async def snapshot(use_vision: bool = False, image_file: str = None, output_mode: str = "stream", output_path: str = None, window_title: str = None, ctx: Context = None):
    """
    Captures the current screen or loads an image file and optionally returns it for LLM analysis.

    This tool mimics the Windows-MCP Snapshot functionality but works cross-platform.
    It captures the current screen state and can optionally return a screenshot image
    that can be analyzed by LLMs when use_vision is True. Alternatively, you can provide
    an image file path to load and analyze.

    Args:
        use_vision: If True, includes an image in the response for visual analysis
        image_file: Optional file path to load an image from instead of capturing screen
        output_mode: Output destination - "stream" returns image in response (default), "file" writes to output_path
        output_path: File path to save the image when output_mode is "file"
        window_title: Optional title of a specific window to capture instead of the full screen.
                      Supports partial matching. Only supported on Windows.

    Returns:
        A list containing system information text and optionally an Image object or file path
    """
    try:
        # Create the main response with system information
        response_parts = []
        
        # If vision is requested, capture and process the screenshot or load from file
        if use_vision:
            try:
                if image_file:
                    # Load image from file
                    if not os.path.exists(image_file):
                        error_msg = f"Image file does not exist: {image_file}"
                        if ctx is not None:
                            ctx.error(error_msg)
                        logger.error(error_msg)
                        response_parts.append(f"\nNote: Could not load image - {error_msg}")
                    else:
                        screenshot = PILImage.open(image_file)
                        logger.debug(f"Loaded image from file: {image_file}")
                elif window_title:
                    # Capture specific window
                    screenshot, matched_title = capture_window_by_title(window_title)
                    logger.debug(f"Captured window: {matched_title}")
                else:
                    # Capture the screenshot
                    screenshot = get_platform_screenshot()
                    logger.debug(f"Captured screenshot from screen")
                
                # Resize the image to prevent oversized images (following Windows-MCP approach)
                MAX_SCREENSHOT_WIDTH, MAX_SCREENSHOT_HEIGHT = 1920, 1080
                screenshot_width, screenshot_height = screenshot.size
                
                # Calculate scale factor to cap resolution at 1080p
                scale_width = MAX_SCREENSHOT_WIDTH / screenshot_width if screenshot_width > MAX_SCREENSHOT_WIDTH else 1.0
                scale_height = MAX_SCREENSHOT_HEIGHT / screenshot_height if screenshot_height > MAX_SCREENSHOT_HEIGHT else 1.0
                scale = min(scale_width, scale_height)  # Use the smaller scale to ensure both dimensions fit
                
                new_width, new_height = screenshot_width, screenshot_height  # Initialize variables
                
                if scale < 1.0:
                    new_width = int(screenshot_width * scale)
                    new_height = int(screenshot_height * scale)
                    screenshot = screenshot.resize((new_width, new_height), PILImage.LANCZOS)
                
                # Process the screenshot using existing image processing pipeline
                img_byte_arr = BytesIO()
                screenshot.save(img_byte_arr, format='PNG')  # Using PNG to preserve quality
                img_data = img_byte_arr.getvalue()

                if output_mode == "file":
                    output_result = await format_image_output(
                        img_data, 'png', output_mode, output_path, ctx,
                        "Screenshot saved."
                    )
                    if "text" in output_result:
                        response_parts.append(output_result["text"])
                    elif "error" in output_result:
                        response_parts.append(f"\nNote: Could not save image - {output_result['error']}")
                else:
                    # Create an Image object to return to the LLM
                    image_obj = Image(data=img_data, format='png')
                    response_parts.append(image_obj)

                logger.debug(f"Image processed: {screenshot_width}x{screenshot_height} -> {new_width}x{new_height}")
                
            except Exception as e:
                error_msg = f"Error processing image: {str(e)}"
                if ctx is not None:
                    ctx.error(error_msg)
                logger.error(error_msg)
                response_parts.append(f"\nNote: Could not process image - {str(e)}")
        
        return response_parts
        
    except Exception as e:
        error_msg = f"Error in snapshot tool: {str(e)}"
        ctx.error(error_msg)
        logger.exception(error_msg)
        return [f"Error capturing system state: {str(e)}"]


@mcp.tool()
async def list_windows(ctx: Context = None):
    """
    List all visible open windows with their titles and positions.

    This tool returns a list of visible windows on the system, including their
    titles and bounding box coordinates. Use this to discover window titles
    for the snapshot tool's window_title parameter.

    Returns:
        A list of text descriptions of visible windows.
    """
    try:
        windows = get_window_list()
        
        if not windows:
            import platform
            system = platform.system()
            if system != "Windows":
                return [f"Window listing is only supported on Windows. Current platform: {system}"]
            return ["No visible windows found."]
        
        response_parts = [f"Found {len(windows)} visible window(s):"]
        
        for i, win in enumerate(windows, 1):
            response_parts.append(
                f"{i}. Title: \"{win['title']}\" | "
                f"Position: ({win['left']}, {win['top']}) | "
                f"Size: {win['width']}x{win['height']}"
            )
        
        logger.debug(f"Listed {len(windows)} windows")
        return response_parts
        
    except Exception as e:
        error_msg = f"Error listing windows: {str(e)}"
        if ctx is not None:
            ctx.error(error_msg)
        logger.exception(error_msg)
        return [f"Error listing windows: {str(e)}"]


@mcp.tool()
async def mouse_rect(width: int = 100, height: int = 100, output_mode: str = "stream", output_path: str = None, ctx: Context = None):
    """
    Captures a small rectangle around the current mouse position and returns it as an image.

    This tool captures a screenshot of a rectangular area centered on the current mouse cursor
    position, with the specified width and height. It's useful for getting a close-up view
    of what's currently under the mouse pointer.

    Args:
        width: Width of the rectangle to capture (default: 100 pixels)
        height: Height of the rectangle to capture (default: 100 pixels)
        output_mode: Output destination - "stream" returns image in response (default), "file" writes to output_path
        output_path: File path to save the image when output_mode is "file"
        ctx: MCP context for logging and error reporting

    Returns:
        A list containing a text description and the captured Image object or file path
    """
    try:
        # Get mouse position using cross-platform approach
        try:
            import pyautogui
            x, y = pyautogui.position()
        except ImportError:
            # Fallback: try to get position using platform-specific methods
            import platform
            system = platform.system()
            if system == "Windows":
                try:
                    import win32gui
                    point = win32gui.GetCursorPos()
                    x, y = point
                except ImportError:
                    # Last resort: use ctypes
                    import ctypes
                    class POINT(ctypes.Structure):
                        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
                    pt = POINT()
                    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                    x, y = pt.x, pt.y
            elif system == "Darwin":  # macOS
                try:
                    from Quartz import CGEventCreate, CGEventGetLocation
                    event = CGEventCreate(None)
                    point = CGEventGetLocation(event)
                    x, y = point.x, point.y
                except ImportError:
                    raise RuntimeError("Unable to get mouse position on macOS without Quartz")
            else:  # Linux and others
                try:
                    import Xlib.display
                    display = Xlib.display.Display()
                    root = display.screen().root
                    data = root.query_pointer()._data
                    x, y = data["root_x"], data["root_y"]
                except ImportError:
                    raise RuntimeError("Unable to get mouse position on Linux without Xlib")
        
        # Calculate the rectangle boundaries
        left = max(0, x - width // 2)
        top = max(0, y - height // 2)
        right = left + width
        bottom = top + height
        
        # Capture the screen
        screenshot = get_platform_screenshot()
        
        # Crop to the desired rectangle
        # Ensure we don't go beyond screen boundaries
        screen_width, screen_height = screenshot.size
        left = min(left, screen_width)
        top = min(top, screen_height)
        right = min(right, screen_width)
        bottom = min(bottom, screen_height)
        
        # Only crop if we have a valid rectangle
        if right > left and bottom > top:
            screenshot = screenshot.crop((left, top, right, bottom))
        else:
            # If the rectangle is invalid, capture a small area around the mouse
            # but adjust to stay within bounds
            size = min(50, screen_width // 4, screen_height // 4)  # Default to 50px or 1/4 screen
            left = max(0, min(x - size // 2, screen_width - size))
            top = max(0, min(y - size // 2, screen_height - size))
            right = left + size
            bottom = top + size
            screenshot = screenshot.crop((left, top, right, bottom))
        
        logger.debug(f"Captured mouse rectangle: {left},{top} to {right},{bottom} (size: {screenshot.size})")
        
        # Process the screenshot using existing image processing pipeline
        img_byte_arr = BytesIO()
        screenshot.save(img_byte_arr, format='PNG')  # Using PNG to preserve quality
        img_data = img_byte_arr.getvalue()

        description = f"Captured {screenshot.size[0]}x{screenshot.size[1]} pixel rectangle around mouse position ({x}, {y})"

        if output_mode == "file":
            output_result = await format_image_output(
                img_data, 'png', output_mode, output_path, ctx, description
            )
            if "text" in output_result:
                return [output_result["text"]]
            elif "error" in output_result:
                return [f"Error: {output_result['error']}"]

        # Create an Image object to return to the LLM
        image_obj = Image(data=img_data, format='png')

        # Return response with description and image
        return [
            description,
            image_obj
        ]

    except Exception as e:
        error_msg = f"Error capturing mouse rectangle: {str(e)}"
        if ctx is not None:
            ctx.error(error_msg)
        logger.exception(error_msg)
        return [f"Error capturing mouse rectangle: {str(e)}"]


@mcp.tool()
async def mouse_click(x: int, y: int, button: str = "left", ctx: Context = None):
    """
    Move the mouse to a specified position and click.

    This tool moves the mouse cursor to the given coordinates and performs a click
    using the specified mouse button.

    Args:
        x: X coordinate to click
        y: Y coordinate to click
        button: Mouse button to click, one of left, right, or middle
        ctx: MCP context for logging and error reporting

    Returns:
        A list containing a text description of the click action
    """
    try:
        normalized_button = button.lower()
        if normalized_button not in {"left", "right", "middle"}:
            raise ValueError(f"Invalid button '{button}'. Expected left, right, or middle.")

        try:
            import pyautogui

            pyautogui.moveTo(x, y)
            pyautogui.click(button=normalized_button)
        except ImportError:
            import platform

            system = platform.system()
            if system == "Windows":
                import ctypes

                ctypes.windll.user32.SetCursorPos(x, y)
                button_flags = {
                    "left": (0x0002, 0x0004),
                    "right": (0x0008, 0x0010),
                    "middle": (0x0020, 0x0040),
                }
                down_flag, up_flag = button_flags[normalized_button]
                ctypes.windll.user32.mouse_event(down_flag, 0, 0, 0, 0)
                ctypes.windll.user32.mouse_event(up_flag, 0, 0, 0, 0)
            elif system == "Darwin":
                raise RuntimeError("Mouse clicking without pyautogui is not implemented on macOS.")
            else:
                raise RuntimeError("Mouse clicking without pyautogui is not implemented on this platform.")

        logger.debug(f"Clicked {normalized_button} mouse button at position ({x}, {y})")
        return [f"Clicked the {normalized_button} mouse button at ({x}, {y})"]

    except Exception as e:
        error_msg = f"Error clicking mouse: {str(e)}"
        if ctx is not None:
            ctx.error(error_msg)
        logger.exception(error_msg)
        return [f"Error clicking mouse: {str(e)}"]


@mcp.tool()
async def mouse_move_screenshot(x: int, y: int, width: int = 200, height: int = 200, output_mode: str = "stream", output_path: str = None, ctx: Context = None):
    """
    Move the mouse to a specified position and return a screenshot at the target position with a visual indicator.

    This tool moves the mouse cursor to the specified coordinates, captures a screenshot
    around that position, and adds a visual indicator (red crosshair) to show the exact
    mouse position since cursor capture is not always possible with screenshot methods.

    Args:
        x: X coordinate to move mouse to
        y: Y coordinate to move mouse to
        width: Width of the rectangle to capture around the position (default: 200 pixels)
        height: Height of the rectangle to capture around the position (default: 200 pixels)
        output_mode: Output destination - "stream" returns image in response (default), "file" writes to output_path
        output_path: File path to save the image when output_mode is "file"
        ctx: MCP context for logging and error reporting

    Returns:
        A list containing a text description and the captured Image object with visual indicator or file path
    """
    try:
        # Move mouse to specified position
        try:
            import pyautogui
            pyautogui.moveTo(x, y)
            logger.debug(f"Moved mouse to position ({x}, {y})")
        except ImportError:
            # Fallback: try to move position using platform-specific methods
            import platform
            system = platform.system()
            if system == "Windows":
                try:
                    import ctypes
                    ctypes.windll.user32.SetCursorPos(x, y)
                except Exception as e:
                    raise RuntimeError(f"Unable to move mouse on Windows: {str(e)}")
            elif system == "Darwin":  # macOS
                try:
                    from Quartz import CGEventCreateMouseEvent, CGEventPost, kCGHIDEventTap
                    event = CGEventCreateMouseEvent(None, kCGEventMouseMoved, (x, y), 0)
                    CGEventPost(kCGHIDEventTap, event)
                except ImportError:
                    raise RuntimeError("Unable to move mouse on macOS without Quartz")
            else:  # Linux and others
                try:
                    import Xlib.display
                    import Xlib.X
                    display = Xlib.display.Display()
                    root = display.screen().root
                    # Warp the pointer
                    root.warp_pointer(x, y)
                    display.sync()
                except ImportError:
                    raise RuntimeError("Unable to move mouse on Linux without Xlib")
        
        # Small delay to ensure mouse movement completes
        import asyncio
        await asyncio.sleep(0.1)
        
        # Get mouse position after movement to confirm
        try:
            import pyautogui
            actual_x, actual_y = pyautogui.position()
        except ImportError:
            # Fallback: try to get position using platform-specific methods
            import platform
            system = platform.system()
            if system == "Windows":
                try:
                    import win32gui
                    point = win32gui.GetCursorPos()
                    actual_x, actual_y = point
                except ImportError:
                    # Last resort: use ctypes
                    import ctypes
                    class POINT(ctypes.Structure):
                        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]
                    pt = POINT()
                    ctypes.windll.user32.GetCursorPos(ctypes.byref(pt))
                    actual_x, actual_y = pt.x, pt.y
            elif system == "Darwin":  # macOS
                try:
                    from Quartz import CGEventCreate, CGEventGetLocation
                    event = CGEventCreate(None)
                    point = CGEventGetLocation(event)
                    actual_x, actual_y = point.x, point.y
                except ImportError:
                    raise RuntimeError("Unable to get mouse position on macOS without Quartz")
            else:  # Linux and others
                try:
                    import Xlib.display
                    display = Xlib.display.Display()
                    root = display.screen().root
                    data = root.query_pointer()._data
                    actual_x, actual_y = data["root_x"], data["root_y"]
                except ImportError:
                    raise RuntimeError("Unable to get mouse position on Linux without Xlib")
        
        logger.debug(f"Mouse moved to confirmed position ({actual_x}, {actual_y})")
        
        # Calculate the rectangle boundaries for screenshot
        left = max(0, actual_x - width // 2)
        top = max(0, actual_y - height // 2)
        right = left + width
        bottom = top + height
        
        # Capture the screen
        screenshot = get_platform_screenshot()
        
        # Crop to the desired rectangle
        # Ensure we don't go beyond screen boundaries
        screen_width, screen_height = screenshot.size
        left = min(left, screen_width)
        top = min(top, screen_height)
        right = min(right, screen_width)
        bottom = min(bottom, screen_height)
        
        # Only crop if we have a valid rectangle
        if right > left and bottom > top:
            screenshot = screenshot.crop((left, top, right, bottom))
        else:
            # If the rectangle is invalid, capture a small area around the mouse
            # but adjust to stay within bounds
            size = min(50, screen_width // 4, screen_height // 4)  # Default to 50px or 1/4 screen
            left = max(0, min(actual_x - size // 2, screen_width - size))
            top = max(0, min(actual_y - size // 2, screen_height - size))
            right = left + size
            bottom = top + size
            screenshot = screenshot.crop((left, top, right, bottom))
        
        # Add visual indicator (red crosshair) at the mouse position
        from PIL import ImageDraw
        draw = ImageDraw.Draw(screenshot)
        
        # Calculate position of mouse within the cropped screenshot
        mouse_x_in_screenshot = actual_x - left
        mouse_y_in_screenshot = actual_y - top
        
        # Draw crosshair
        crosshair_size = 10
        crosshair_width = 2
        red_color = (255, 0, 0)  # Red
        
        # Horizontal line
        draw.line([
            (mouse_x_in_screenshot - crosshair_size, mouse_y_in_screenshot),
            (mouse_x_in_screenshot + crosshair_size, mouse_y_in_screenshot)
        ], fill=red_color, width=crosshair_width)
        
        # Vertical line
        draw.line([
            (mouse_x_in_screenshot, mouse_y_in_screenshot - crosshair_size),
            (mouse_x_in_screenshot, mouse_y_in_screenshot + crosshair_size)
        ], fill=red_color, width=crosshair_width)
        
        # Optional: draw a circle around the center
        circle_radius = 15
        draw.ellipse([
            (mouse_x_in_screenshot - circle_radius, mouse_y_in_screenshot - circle_radius),
            (mouse_x_in_screenshot + circle_radius, mouse_y_in_screenshot + circle_radius)
        ], outline=red_color, width=crosshair_width)
        
        logger.debug(f"Captured mouse move screenshot: {left},{top} to {right},{bottom} (size: {screenshot.size})")
        
        # Process the screenshot using existing image processing pipeline
        img_byte_arr = BytesIO()
        screenshot.save(img_byte_arr, format='PNG')  # Using PNG to preserve quality
        img_data = img_byte_arr.getvalue()

        description = f"Moved mouse to ({actual_x}, {actual_y}) and captured {screenshot.size[0]}x{screenshot.size[1]} pixel rectangle with visual indicator"

        if output_mode == "file":
            output_result = await format_image_output(
                img_data, 'png', output_mode, output_path, ctx, description
            )
            if "text" in output_result:
                return [output_result["text"]]
            elif "error" in output_result:
                return [f"Error: {output_result['error']}"]

        # Create an Image object to return to the LLM
        image_obj = Image(data=img_data, format='png')

        # Return response with description and image
        return [
            description,
            image_obj
        ]

    except Exception as e:
        error_msg = f"Error in mouse move screenshot: {str(e)}"
        if ctx is not None:
            ctx.error(error_msg)
        logger.exception(error_msg)
        return [f"Error in mouse move screenshot: {str(e)}"]


def main():
    mcp.run(transport='stdio')

if __name__ == "__main__":
    mcp.run(transport='stdio')
