# MCP Images

A cross-platform image processing and screen capture MCP server that enables AI agents to interact with images and capture screenshots.

## Features

- **Image Fetching**: Fetch and process images from URLs or local file paths
- **Cross-Platform Screenshot**: Capture screenshots on Windows, macOS, and Linux
- **Image Compression**: Automatically compress large images while maintaining quality
- **Vision Support**: Return images in a format suitable for LLM vision models

## Tools

### `fetch_images`

Fetch and process images from URLs or local file paths, returning them in a format suitable for LLMs.

```python
# returns MCP Image objects (default)
await fetch_images(image_sources: List[str])

# write processed images to Temp/ and return file paths (useful for UI previews / snapshot)
await fetch_images(image_sources: List[str], save_to_temp=True)
```

### `snapshot`

Captures the current screen or loads an image file and optionally returns it for LLM analysis.

This tool mimics the Windows-MCP Snapshot functionality but works cross-platform. It captures the current screen state and can optionally return a screenshot image that can be analyzed by LLMs when `use_vision` is True. Alternatively, you can provide an image file path to load and analyze.

```python
await snapshot(use_vision: bool = False, image_file: str = None)
```

Parameters:
- `use_vision`: If True, includes an image in the response for visual analysis
- `image_file`: Optional file path to load an image from instead of capturing screen

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