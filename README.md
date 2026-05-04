# MCP Images

A cross-platform image processing and screen capture MCP server that enables AI agents to interact with images and capture screenshots. Derived from https://github.com/IA-Programming/mcp-images

## Features

- **Image Fetching**: Fetch and process images from URLs or local file paths
- **Cross-Platform Screenshot**: Capture screenshots on Windows, macOS, and Linux
- **Image Compression**: Automatically compress large images while maintaining quality
- **Vision Support**: Return images in a format suitable for LLM vision models
- **Flexible Output**: Stream images directly in the response or save to file paths

## Tools

### `fetch_images`

Fetch and process images from URLs or local file paths, returning them in a format suitable for LLMs.

```python
# returns MCP Image objects (default)
await fetch_images(image_sources: List[str])

# write processed images to file paths
await fetch_images(image_sources: List[str], output_mode="file", output_path="C:/images/output.png")
await fetch_images(image_sources: List[str], output_mode="file", output_path="C:/images/output_dir/")
```

Parameters:
- `image_sources`: A list of image URLs or local file paths
- `output_mode`: Output destination - `"stream"` returns image in response (default), `"file"` writes to `output_path`
- `output_path`: File path or directory path for file output mode. For multiple images, this should be a directory. For a single image, can be a specific file path.

### `snapshot`

Captures the current screen or loads an image file and optionally returns it for LLM analysis.

This tool mimics the Windows-MCP Snapshot functionality but works cross-platform. It captures the current screen state and can optionally return a screenshot image that can be analyzed by LLMs when `use_vision` is True. Alternatively, you can provide an image file path to load and analyze.

```python
# Stream mode (default) - returns image in response
await snapshot(use_vision: bool = False, image_file: str = None)

# File mode - saves image to specified path
await snapshot(use_vision=True, output_mode="file", output_path="C:/screenshots/screen.png")
```

Parameters:
- `use_vision`: If True, includes an image in the response for visual analysis
- `image_file`: Optional file path to load an image from instead of capturing screen
- `output_mode`: Output destination - `"stream"` returns image in response (default), `"file"` writes to `output_path`
- `output_path`: File path to save the image when `output_mode` is `"file"`

### `mouse_rect`

Captures a small rectangle around the current mouse position and returns it as an image.

This tool captures a screenshot of a rectangular area centered on the current mouse cursor
position, with the specified width and height. It's useful for getting a close-up view
of what's currently under the mouse pointer.

```python
# Stream mode (default)
await mouse_rect(width: int = 100, height: int = 100)

# File mode
await mouse_rect(width=200, height=200, output_mode="file", output_path="C:/screenshots/mouse_rect.png")
```

Parameters:
- `width`: Width of the rectangle to capture (default: 100 pixels)
- `height`: Height of the rectangle to capture (default: 100 pixels)
- `output_mode`: Output destination - `"stream"` returns image in response (default), `"file"` writes to `output_path`
- `output_path`: File path to save the image when `output_mode` is `"file"`

### `mouse_move_screenshot`

Move the mouse to a specified position and return a screenshot at the target position with a visual indicator.

This tool moves the mouse cursor to the specified coordinates, captures a screenshot
around that position, and adds a visual indicator (red crosshair) to show the exact
mouse position since cursor capture is not always possible with screenshot methods.

```python
# Stream mode (default)
await mouse_move_screenshot(x: int, y: int, width: int = 200, height: int = 200)

# File mode
await mouse_move_screenshot(x=100, y=200, output_mode="file", output_path="C:/screenshots/move_screenshot.png")
```

Parameters:
- `x`: X coordinate to move mouse to
- `y`: Y coordinate to move mouse to
- `width`: Width of the rectangle to capture around the position (default: 200 pixels)
- `height`: Height of the rectangle to capture around the position (default: 200 pixels)
- `output_mode`: Output destination - `"stream"` returns image in response (default), `"file"` writes to `output_path`
- `output_path`: File path to save the image when `output_mode` is `"file"`

### `mouse_click`

Move the mouse to a specified position and click.

This tool moves the mouse cursor to the specified coordinates and performs a click
using the requested button.

```python
await mouse_click(x: int, y: int, button: str = "left")
```

Parameters:
- `x`: X coordinate to click
- `y`: Y coordinate to click
- `button`: Mouse button to click, one of `left`, `right`, or `middle`

## Installation

```bash
pip install -e .
```

## Usage

```
{
  "mcpServers": {
    "image": {
      "command": "uv",
        "args": ["--directory", "/path/to/mcp-image", "run", "mcp_image.py"]
    }
  }
}
```

The server can be run as an MCP server:

```bash
mcp-image
```

Or programmatically:

```python
from mcp_image import main
main()
```

## Dependencies

- httpx
- mcp
- pillow
- mss
- pyautogui
- psutil

## License

MIT
