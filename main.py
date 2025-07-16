#!/usr/bin/env python3
"""
SnapTube-like Media Downloader for Linux
Built with Flet UI and yt-dlp backend
"""

import flet as ft
import sqlite3
import subprocess
import json
import os
import sys
import threading
import time
import re
import signal
from pathlib import Path
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict
from urllib.parse import urlparse
import pyperclip
import psutil

# Configuration
APP_NAME = "SnapTube Downloader"
DB_NAME = "snaptube.db"
CONFIG_DIR = Path.home() / ".snaptube"
CONFIG_DIR.mkdir(exist_ok=True)
DB_PATH = CONFIG_DIR / DB_NAME

@dataclass
class DownloadItem:
    id: Optional[int] = None
    url: str = ""
    title: str = ""
    platform: str = ""
    format_id: str = ""
    quality: str = ""
    file_type: str = ""
    file_path: str = ""
    file_size: int = 0
    progress: float = 0.0
    status: str = "pending"  # pending, downloading, completed, failed, paused, cancelled
    created_at: str = ""
    completed_at: str = ""
    speed: str = ""
    eta: str = ""
    error_message: str = ""
    thumbnail_url: str = ""
    duration: str = ""

class DatabaseManager:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Downloads table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS downloads (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                title TEXT,
                platform TEXT,
                format_id TEXT,
                quality TEXT,
                file_type TEXT,
                file_path TEXT,
                file_size INTEGER DEFAULT 0,
                progress REAL DEFAULT 0.0,
                status TEXT DEFAULT 'pending',
                created_at TEXT,
                completed_at TEXT,
                speed TEXT,
                eta TEXT,
                error_message TEXT,
                thumbnail_url TEXT,
                duration TEXT
            )
        ''')
        
        # Settings table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        ''')
        
        # Set default settings
        default_settings = {
            'download_path': str(Path.home() / 'Downloads'),
            'preferred_quality': 'best',
            'preferred_format': 'mp4',
            'max_concurrent_downloads': '3',
            'max_download_speed': '0',  # 0 means unlimited
            'auto_filename': 'true',
            'remember_preferences': 'true',
            'clipboard_monitoring': 'true'
        }
        
        for key, value in default_settings.items():
            cursor.execute('INSERT OR IGNORE INTO settings (key, value) VALUES (?, ?)', (key, value))
        
        conn.commit()
        conn.close()
    
    def add_download(self, item: DownloadItem) -> int:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO downloads 
            (url, title, platform, format_id, quality, file_type, file_path, 
             file_size, progress, status, created_at, thumbnail_url, duration)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            item.url, item.title, item.platform, item.format_id, item.quality,
            item.file_type, item.file_path, item.file_size, item.progress,
            item.status, item.created_at, item.thumbnail_url, item.duration
        ))
        
        download_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return download_id
    
    def update_download(self, download_id: int, **kwargs):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        set_clause = ', '.join([f'{key} = ?' for key in kwargs.keys()])
        values = list(kwargs.values()) + [download_id]
        
        cursor.execute(f'UPDATE downloads SET {set_clause} WHERE id = ?', values)
        conn.commit()
        conn.close()
    
    def get_downloads(self, status: Optional[str] = None) -> List[DownloadItem]:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        if status:
            cursor.execute('SELECT * FROM downloads WHERE status = ? ORDER BY created_at DESC', (status,))
        else:
            cursor.execute('SELECT * FROM downloads ORDER BY created_at DESC')
        
        rows = cursor.fetchall()
        conn.close()
        
        downloads = []
        for row in rows:
            download = DownloadItem(
                id=row[0], url=row[1], title=row[2], platform=row[3],
                format_id=row[4], quality=row[5], file_type=row[6],
                file_path=row[7], file_size=row[8], progress=row[9],
                status=row[10], created_at=row[11], completed_at=row[12],
                speed=row[13], eta=row[14], error_message=row[15],
                thumbnail_url=row[16], duration=row[17]
            )
            downloads.append(download)
        
        return downloads
    
    def get_download_by_id(self, download_id: int) -> Optional[DownloadItem]:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM downloads WHERE id = ?', (download_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return DownloadItem(
                id=row[0], url=row[1], title=row[2], platform=row[3],
                format_id=row[4], quality=row[5], file_type=row[6],
                file_path=row[7], file_size=row[8], progress=row[9],
                status=row[10], created_at=row[11], completed_at=row[12],
                speed=row[13], eta=row[14], error_message=row[15],
                thumbnail_url=row[16], duration=row[17]
            )
        return None
    
    def delete_download(self, download_id: int):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('DELETE FROM downloads WHERE id = ?', (download_id,))
        conn.commit()
        conn.close()
    
    def get_setting(self, key: str) -> str:
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('SELECT value FROM settings WHERE key = ?', (key,))
        result = cursor.fetchone()
        conn.close()
        return result[0] if result else ""
    
    def set_setting(self, key: str, value: str):
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute('INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)', (key, value))
        conn.commit()
        conn.close()

class YTDLPManager:
    def __init__(self, db_manager: DatabaseManager):
        self.db_manager = db_manager
        self.active_downloads: Dict[int, subprocess.Popen] = {}
        self.download_threads: Dict[int, threading.Thread] = {}
        self.max_concurrent = int(db_manager.get_setting('max_concurrent_downloads'))
        self.max_speed = db_manager.get_setting('max_download_speed')
    
    def get_video_info(self, url: str) -> Dict:
        """Extract video information using yt-dlp"""
        try:
            cmd = [
                'yt-dlp',
                '--dump-json',
                '--no-download',
                '--flat-playlist',
                url
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                return json.loads(result.stdout)
            else:
                return {'error': result.stderr}
        except Exception as e:
            return {'error': str(e)}
    
    def get_available_formats(self, url: str) -> List[Dict]:
        """Get available formats for a video"""
        try:
            cmd = [
                'yt-dlp',
                '--list-formats',
                '--dump-json',
                url
            ]
            
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            
            if result.returncode == 0:
                info = json.loads(result.stdout)
                return info.get('formats', [])
            else:
                return []
        except Exception as e:
            return []
    
    def start_download(self, download_id: int, url: str, format_id: str, output_path: str, 
                      progress_callback=None):
        """Start a download in a separate thread"""
        if len(self.active_downloads) >= self.max_concurrent:
            return False
        
        def download_worker():
            try:
                self.db_manager.update_download(download_id, status='downloading')
                
                # Build yt-dlp command
                cmd = [
                    'yt-dlp',
                    '-f', format_id,
                    '-o', output_path,
                    '--newline',
                    '--no-playlist'
                ]
                
                # Add speed limit if set
                if self.max_speed != '0':
                    cmd.extend(['--limit-rate', f'{self.max_speed}K'])
                
                cmd.append(url)
                
                # Start download process
                process = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    universal_newlines=True,
                    bufsize=1
                )
                
                self.active_downloads[download_id] = process
                
                # Monitor progress
                while True:
                    output = process.stdout.readline()
                    if output == '' and process.poll() is not None:
                        break
                    
                    if output:
                        self.parse_progress(download_id, output.strip(), progress_callback)
                
                # Check final status
                if process.returncode == 0:
                    self.db_manager.update_download(
                        download_id,
                        status='completed',
                        progress=100.0,
                        completed_at=datetime.now().isoformat()
                    )
                else:
                    self.db_manager.update_download(
                        download_id,
                        status='failed',
                        error_message='Download failed'
                    )
                
            except Exception as e:
                self.db_manager.update_download(
                    download_id,
                    status='failed',
                    error_message=str(e)
                )
            
            finally:
                # Clean up
                if download_id in self.active_downloads:
                    del self.active_downloads[download_id]
                if download_id in self.download_threads:
                    del self.download_threads[download_id]
                
                if progress_callback:
                    progress_callback(download_id)
        
        # Start download thread
        thread = threading.Thread(target=download_worker)
        self.download_threads[download_id] = thread
        thread.daemon = True
        thread.start()
        
        return True
    
    def parse_progress(self, download_id: int, output: str, progress_callback=None):
        """Parse yt-dlp output for progress information"""
        try:
            # Parse download progress
            if '[download]' in output:
                # Extract percentage
                percent_match = re.search(r'(\d+\.?\d*)%', output)
                if percent_match:
                    progress = float(percent_match.group(1))
                    
                    # Extract speed
                    speed_match = re.search(r'at\s+([0-9.]+[KMG]?iB/s)', output)
                    speed = speed_match.group(1) if speed_match else ""
                    
                    # Extract ETA
                    eta_match = re.search(r'ETA\s+([0-9:]+)', output)
                    eta = eta_match.group(1) if eta_match else ""
                    
                    # Update database
                    self.db_manager.update_download(
                        download_id,
                        progress=progress,
                        speed=speed,
                        eta=eta
                    )
                    
                    if progress_callback:
                        progress_callback(download_id)
        
        except Exception as e:
            pass
    
    def pause_download(self, download_id: int):
        """Pause a download"""
        if download_id in self.active_downloads:
            process = self.active_downloads[download_id]
            try:
                process.send_signal(signal.SIGSTOP)
                self.db_manager.update_download(download_id, status='paused')
            except:
                pass
    
    def resume_download(self, download_id: int):
        """Resume a paused download"""
        if download_id in self.active_downloads:
            process = self.active_downloads[download_id]
            try:
                process.send_signal(signal.SIGCONT)
                self.db_manager.update_download(download_id, status='downloading')
            except:
                pass
    
    def cancel_download(self, download_id: int):
        """Cancel a download"""
        if download_id in self.active_downloads:
            process = self.active_downloads[download_id]
            try:
                process.terminate()
                self.db_manager.update_download(download_id, status='cancelled')
            except:
                pass
    
    def retry_download(self, download_id: int, progress_callback=None):
        """Retry a failed download"""
        download = self.db_manager.get_download_by_id(download_id)
        if download:
            self.start_download(
                download_id,
                download.url,
                download.format_id,
                download.file_path,
                progress_callback
            )

class ClipboardMonitor:
    def __init__(self, callback):
        self.callback = callback
        self.running = False
        self.thread = None
        self.last_clipboard = ""
    
    def start(self):
        self.running = True
        self.thread = threading.Thread(target=self._monitor_loop)
        self.thread.daemon = True
        self.thread.start()
    
    def stop(self):
        self.running = False
        if self.thread:
            self.thread.join()
    
    def _monitor_loop(self):
        while self.running:
            try:
                current_clipboard = pyperclip.paste()
                if current_clipboard != self.last_clipboard:
                    self.last_clipboard = current_clipboard
                    if self._is_video_url(current_clipboard):
                        self.callback(current_clipboard)
                time.sleep(1)
            except Exception:
                pass
    
    def _is_video_url(self, text: str) -> bool:
        """Check if text is a video URL"""
        video_domains = [
            'youtube.com', 'youtu.be', 'vimeo.com', 'dailymotion.com',
            'twitch.tv', 'facebook.com', 'instagram.com', 'tiktok.com'
        ]
        
        try:
            parsed = urlparse(text)
            return any(domain in parsed.netloc for domain in video_domains)
        except:
            return False

class SnapTubeApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.db_manager = DatabaseManager(DB_PATH)
        self.ytdlp_manager = YTDLPManager(self.db_manager)
        self.clipboard_monitor = ClipboardMonitor(self.on_clipboard_url)
        
        self.setup_page()
        self.setup_ui()
        self.start_clipboard_monitor()
    
    def setup_page(self):
        self.page.title = APP_NAME
        self.page.window_width = 1000
        self.page.window_height = 700
        self.page.window_min_width = 800
        self.page.window_min_height = 600
        self.page.theme_mode = ft.ThemeMode.LIGHT
        self.page.padding = 0
    
    def setup_ui(self):
        # Main tabs
        self.tabs = ft.Tabs(
            selected_index=0,
            animation_duration=300,
            tabs=[
                ft.Tab(
                    text="Add Download",
                    icon=ft.Icons.DOWNLOAD,
                    content=self.create_add_download_tab()
                ),
                ft.Tab(
                    text="Downloading",
                    icon=ft.Icons.DOWNLOADING,
                    content=self.create_downloading_tab()
                ),
                ft.Tab(
                    text="Downloaded",
                    icon=ft.Icons.DOWNLOAD_DONE,
                    content=self.create_downloaded_tab()
                ),
                ft.Tab(
                    text="History",
                    icon=ft.Icons.HISTORY,
                    content=self.create_history_tab()
                ),
                ft.Tab(
                    text="Settings",
                    icon=ft.Icons.SETTINGS,
                    content=self.create_settings_tab()
                )
            ]
        )
        
        self.page.add(self.tabs)
    
    def create_add_download_tab(self):
        self.url_input = ft.TextField(
            label="Video URL",
            hint_text="Paste YouTube, Vimeo, or other video URL here",
            width=500,
            on_change=self.on_url_change
        )
        
        self.analyze_button = ft.ElevatedButton(
            text="Analyze",
            icon=ft.Icons.SEARCH,
            on_click=self.analyze_url,
            disabled=True
        )
        
        self.video_info_card = ft.Card(
            visible=False,
            content=ft.Container(
                padding=20,
                content=ft.Column([])
            )
        )
        
        self.format_dropdown = ft.Dropdown(
            label="Format & Quality",
            width=200,
            visible=False
        )
        
        self.download_button = ft.ElevatedButton(
            text="Download",
            icon=ft.Icons.DOWNLOAD,
            on_click=self.start_download,
            visible=False,
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.BLUE,
                color=ft.Colors.WHITE
            )
        )
        
        return ft.Container(
            padding=20,
            content=ft.Column([
                ft.Text("Add New Download", size=24, weight=ft.FontWeight.BOLD),
                ft.Divider(),
                ft.Row([
                    self.url_input,
                    self.analyze_button
                ]),
                self.video_info_card,
                ft.Row([
                    self.format_dropdown,
                    self.download_button
                ])
            ])
        )
    
    def create_downloading_tab(self):
        self.downloading_list = ft.ListView(
            expand=True,
            spacing=10,
            padding=20
        )
        
        return ft.Container(
            padding=20,
            content=ft.Column([
                ft.Row([
                    ft.Text("Active Downloads", size=24, weight=ft.FontWeight.BOLD),
                    # ft.Spacer(),
                    ft.Container(expand=True),
                    ft.IconButton(
                        icon=ft.Icons.REFRESH,
                        on_click=self.refresh_downloading_list
                    )
                ]),
                ft.Divider(),
                self.downloading_list
            ])
        )
    
    def create_downloaded_tab(self):
        self.downloaded_list = ft.ListView(
            expand=True,
            spacing=10,
            padding=20
        )
        
        return ft.Container(
            padding=20,
            content=ft.Column([
                ft.Row([
                    ft.Text("Downloaded Files", size=24, weight=ft.FontWeight.BOLD),
                    # ft.Spacer()
                    ft.Container(expand=True),
                    ft.IconButton(
                        icon=ft.Icons.REFRESH,
                        on_click=self.refresh_downloaded_list
                    )
                ]),
                ft.Divider(),
                self.downloaded_list
            ])
        )
    
    def create_history_tab(self):
        self.history_search = ft.TextField(
            label="Search history",
            prefix_icon=ft.Icons.SEARCH,
            width=300,
            on_change=self.filter_history
        )
        
        self.history_list = ft.ListView(
            expand=True,
            spacing=10,
            padding=20
        )
        
        return ft.Container(
            padding=20,
            content=ft.Column([
                ft.Row([
                    ft.Text("Download History", size=24, weight=ft.FontWeight.BOLD),
                    ft.Container(expand=True),
                    self.history_search,
                    ft.IconButton(
                        icon=ft.Icons.REFRESH,
                        on_click=self.refresh_history_list
                    )
                ]),
                ft.Divider(),
                self.history_list
            ])
        )
    
    def create_settings_tab(self):
        self.download_path_input = ft.TextField(
            label="Download Directory",
            value=self.db_manager.get_setting('download_path'),
            width=400,
            read_only=True
        )
        
        self.browse_button = ft.ElevatedButton(
            text="Browse",
            icon=ft.Icons.FOLDER_OPEN,
            on_click=self.browse_download_path
        )
        
        self.quality_dropdown = ft.Dropdown(
            label="Preferred Quality",
            width=200,
            value=self.db_manager.get_setting('preferred_quality'),
            options=[
                ft.dropdown.Option("best", "Best Quality"),
                ft.dropdown.Option("worst", "Worst Quality"),
                ft.dropdown.Option("720p", "720p"),
                ft.dropdown.Option("480p", "480p"),
                ft.dropdown.Option("360p", "360p"),
            ]
        )
        
        self.format_dropdown_settings = ft.Dropdown(
            label="Preferred Format",
            width=200,
            value=self.db_manager.get_setting('preferred_format'),
            options=[
                ft.dropdown.Option("mp4", "MP4"),
                ft.dropdown.Option("webm", "WebM"),
                ft.dropdown.Option("mp3", "MP3"),
                ft.dropdown.Option("m4a", "M4A"),
            ]
        )
        
        self.max_downloads_input = ft.TextField(
            label="Max Concurrent Downloads",
            value=self.db_manager.get_setting('max_concurrent_downloads'),
            width=200,
            keyboard_type=ft.KeyboardType.NUMBER
        )
        
        self.max_speed_input = ft.TextField(
            label="Max Speed (KB/s, 0 = unlimited)",
            value=self.db_manager.get_setting('max_download_speed'),
            width=200,
            keyboard_type=ft.KeyboardType.NUMBER
        )
        
        self.clipboard_monitoring_switch = ft.Switch(
            label="Monitor Clipboard",
            value=self.db_manager.get_setting('clipboard_monitoring') == 'true'
        )
        
        self.save_settings_button = ft.ElevatedButton(
            text="Save Settings",
            icon=ft.Icons.SAVE,
            on_click=self.save_settings,
            style=ft.ButtonStyle(
                bgcolor=ft.Colors.GREEN,
                color=ft.Colors.WHITE
            )
        )
        
        return ft.Container(
            padding=20,
            content=ft.Column([
                ft.Text("Settings", size=24, weight=ft.FontWeight.BOLD),
                ft.Divider(),
                ft.Row([
                    ft.Text("Download Path:", size=16, weight=ft.FontWeight.BOLD),
                ]),
                ft.Row([
                    self.download_path_input,
                    self.browse_button
                ]),
                ft.Row([
                    ft.Text("Download Preferences:", size=16, weight=ft.FontWeight.BOLD),
                ]),
                ft.Row([
                    self.quality_dropdown,
                    self.format_dropdown_settings,
                ]),
                ft.Row([
                    ft.Text("Performance:", size=16, weight=ft.FontWeight.BOLD),
                ]),
                ft.Row([
                    self.max_downloads_input,
                    self.max_speed_input,
                ]),
                ft.Row([
                    ft.Text("Features:", size=16, weight=ft.FontWeight.BOLD),
                ]),
                self.clipboard_monitoring_switch,
                ft.Divider(),
                self.save_settings_button
            ])
        )
    
    def on_url_change(self, e):
        self.analyze_button.disabled = not bool(e.control.value.strip())
        self.page.update()
    
    def analyze_url(self, e):
        url = self.url_input.value.strip()
        if not url:
            return
        
        # Show loading
        self.analyze_button.disabled = True
        self.analyze_button.text = "Analyzing..."
        self.page.update()
        
        # Run analysis in thread
        def analyze_worker():
            try:
                info = self.ytdlp_manager.get_video_info(url)
                
                if 'error' in info:
                    self.show_error(info['error'])
                    return
                
                # Update UI on main thread
                self.page.run_thread(lambda: self.display_video_info(info))
                
            except Exception as e:
                self.page.run_thread(lambda: self.show_error(str(e)))
            finally:
                self.page.run_thread(lambda: self.reset_analyze_button())
        
        threading.Thread(target=analyze_worker, daemon=True).start()
    
    def display_video_info(self, info):
        # Extract info
        title = info.get('title', 'Unknown')
        uploader = info.get('uploader', 'Unknown')
        duration = info.get('duration_string', 'Unknown')
        platform = info.get('extractor_key', 'Unknown')
        thumbnail = info.get('thumbnail', '')
        
        # Create info display
        info_content = ft.Column([
            ft.Row([
                ft.Icon(ft.Icons.PLAY_CIRCLE_OUTLINE, size=40),
                ft.Column([
                    ft.Text(title, size=18, weight=ft.FontWeight.BOLD),
                    ft.Text(f"By: {uploader}", size=14),
                    ft.Text(f"Duration: {duration} | Platform: {platform}", size=12),
                ])
            ])
        ])
        
        self.video_info_card.content.content = info_content
        self.video_info_card.visible = True
        
        # Get formats
        formats = self.ytdlp_manager.get_available_formats(self.url_input.value)
        self.populate_format_dropdown(formats)
        
        self.page.update()
    
    def populate_format_dropdown(self, formats):
        options = []
        
        # Group formats by type
        video_formats = []
        audio_formats = []
        
        for fmt in formats:
            if fmt.get('vcodec') != 'none' and fmt.get('acodec') != 'none':
                # Video with audio
                quality = fmt.get('format_note', fmt.get('height', 'Unknown'))
                ext = fmt.get('ext', 'unknown')
                filesize = fmt.get('filesize', 0)
                size_str = f" ({filesize // 1024 // 1024}MB)" if filesize else ""
                
                video_formats.append({
                    'id': fmt['format_id'],
                    'text': f"{quality} {ext.upper()}{size_str}",
                    'available': True
                })
            
            elif fmt.get('acodec') != 'none' and fmt.get('vcodec') == 'none':
                # Audio only
                quality = fmt.get('format_note', fmt.get('abr', 'Unknown'))
                ext = fmt.get('ext', 'unknown')
                filesize = fmt.get('filesize', 0)
                size_str = f" ({filesize // 1024 // 1024}MB)" if filesize else ""
                
                audio_formats.append({
                    'id': fmt['format_id'],
                    'text': f"{quality} {ext.upper()}{size_str} (Audio Only)",
                    'available': True
                })
        
        # Create dropdown options
        for fmt in video_formats[:10]:  # Limit to top 10
            options.append(ft.dropdown.Option(fmt['id'], fmt['text']))
        
        for fmt in audio_formats[:5]:  # Limit to top 5
            options.append(ft.dropdown.Option(fmt['id'], fmt['text']))
        
        self.format_dropdown.options = options
        self.format_dropdown.visible = True
        self.download_button.visible = True
        
        # Select best quality by default
        if options:
            self.format_dropdown.value = options[0].key
        
        self.page.update()
    
    def reset_analyze_button(self):
        self.analyze_button.disabled = False
        self.analyze_button.text = "Analyze"
        self.page.update()
    
    def start_download(self, e):
        url = self.url_input.value.strip()
        format_id = self.format_dropdown.value
        
        if not url or not format_id:
            return
        
        # Get video info again for title
        info = self.ytdlp_manager.get_video_info(url)
        title = info.get('title', 'Unknown')
        platform = info.get('extractor_key', 'Unknown')
        duration = info.get('duration_string', '')
        thumbnail = info.get('thumbnail', '')
        
        # Generate filename
        download_path = self.db_manager.get_setting('download_path')
        safe_title = re.sub(r'[<>:"/\\|?*]', '_', title)
        filename = f"{safe_title}.%(ext)s"
        full_path = os.path.join(download_path, filename)
        
        # Create download item
        download_item = DownloadItem(
            url=url,
            title=title,
            platform=platform,
            format_id=format_id,
            quality=self.format_dropdown.options[0].text if self.format_dropdown.options else "",
            file_type=format_id.split('-')[-1] if '-' in format_id else "mp4",
            file_path=full_path,
            status="pending",
            created_at=datetime.now().isoformat(),
            thumbnail_url=thumbnail,
            duration=duration
        )
        
        # Add to database
        download_id = self.db_manager.add_download(download_item)
        
        # Start download
        success = self.ytdlp_manager.start_download(
            download_id,
            url,
            format_id,
            full_path,
            self.on_download_progress
        )
        
        if success:
            self.show_snackbar("Download started successfully!")
            self.tabs.selected_index = 1  # Switch to downloading tab
            self.refresh_downloading_list()
            
            # Clear form
            self.url_input.value = ""
            self.video_info_card.visible = False
            self.format_dropdown.visible = False
            self.download_button.visible = False
            self.analyze_button.disabled = True
            
        else:
            self.show_error("Maximum concurrent downloads reached!")
        
        self.page.update()
    
    def on_download_progress(self, download_id: int):
        """Called when download progress updates"""
        self.page.run_thread(lambda: self.refresh_downloading_list())
    
    def refresh_downloading_list(self, e=None):
        """Refresh the downloading list"""
        downloads = self.db_manager.get_downloads()
        active_downloads = [d for d in downloads if d.status in ['pending', 'downloading', 'paused']]
        
        self.downloading_list.controls.clear()
        
        for download in active_downloads:
            self.downloading_list.controls.append(self.create_download_item(download, show_actions=True))
        
        self.page.update()
    
    def refresh_downloaded_list(self, e=None):
        """Refresh the downloaded list"""
        downloads = self.db_manager.get_downloads('completed')
        
        self.downloaded_list.controls.clear()
        
        for download in downloads:
            self.downloaded_list.controls.append(self.create_download_item(download, show_actions=False))
        
        self.page.update()
    
    def refresh_history_list(self, e=None):
        """Refresh the history list"""
        downloads = self.db_manager.get_downloads()
        
        self.history_list.controls.clear()
        
        for download in downloads:
            self.history_list.controls.append(self.create_download_item(download, show_history=True))
        
        self.page.update()
    
    def filter_history(self, e):
        """Filter history based on search term"""
        search_term = e.control.value.lower()
        downloads = self.db_manager.get_downloads()
        
        filtered_downloads = [
            d for d in downloads 
            if search_term in d.title.lower() or search_term in d.platform.lower()
        ]
        
        self.history_list.controls.clear()
        
        for download in filtered_downloads:
            self.history_list.controls.append(self.create_download_item(download, show_history=True))
        
        self.page.update()
    
    def create_download_item(self, download: DownloadItem, show_actions=False, show_history=False):
        """Create a download item widget"""
        # Status color
        status_colors = {
            'pending': ft.Colors.ORANGE,
            'downloading': ft.Colors.BLUE,
            'completed': ft.Colors.GREEN,
            'failed': ft.Colors.RED,
            'paused': ft.Colors.AMBER,
            'cancelled': ft.Colors.GREY
        }
        
        status_color = status_colors.get(download.status, ft.Colors.GREY)
        
        # Progress bar
        progress_bar = ft.ProgressBar(
            value=download.progress / 100.0,
            visible=download.status in ['downloading', 'paused'],
            color=status_color
        )
        
        # Status info
        status_text = download.status.title()
        if download.status == 'downloading':
            status_text += f" - {download.speed} - ETA: {download.eta}"
        elif download.status == 'failed':
            status_text += f" - {download.error_message}"
        
        # Action buttons
        action_buttons = []
        
        if show_actions:
            if download.status == 'downloading':
                action_buttons.extend([
                    ft.IconButton(
                        icon=ft.Icons.PAUSE,
                        tooltip="Pause",
                        on_click=lambda e, did=download.id: self.pause_download(did)
                    ),
                    ft.IconButton(
                        icon=ft.Icons.STOP,
                        tooltip="Cancel",
                        on_click=lambda e, did=download.id: self.cancel_download(did)
                    )
                ])
            elif download.status == 'paused':
                action_buttons.extend([
                    ft.IconButton(
                        icon=ft.Icons.PLAY_ARROW,
                        tooltip="Resume",
                        on_click=lambda e, did=download.id: self.resume_download(did)
                    ),
                    ft.IconButton(
                        icon=ft.Icons.STOP,
                        tooltip="Cancel",
                        on_click=lambda e, did=download.id: self.cancel_download(did)
                    )
                ])
            elif download.status == 'failed':
                action_buttons.append(
                    ft.IconButton(
                        icon=ft.Icons.REFRESH,
                        tooltip="Retry",
                        on_click=lambda e, did=download.id: self.retry_download(did)
                    )
                )
        
        if download.status == 'completed':
            action_buttons.extend([
                ft.IconButton(
                    icon=ft.Icons.PLAY_ARROW,
                    tooltip="Open File",
                    on_click=lambda e, path=download.file_path: self.open_file(path)
                ),
                ft.IconButton(
                    icon=ft.Icons.FOLDER_OPEN,
                    tooltip="Open Folder",
                    on_click=lambda e, path=download.file_path: self.open_folder(path)
                )
            ])
        
        if show_history:
            action_buttons.extend([
                ft.IconButton(
                    icon=ft.Icons.DOWNLOAD,
                    tooltip="Download Again",
                    on_click=lambda e, url=download.url: self.download_again(url)
                ),
                ft.IconButton(
                    icon=ft.Icons.DELETE,
                    tooltip="Delete from History",
                    on_click=lambda e, did=download.id: self.delete_from_history(did)
                )
            ])
        
        return ft.Card(
            content=ft.Container(
                padding=15,
                content=ft.Column([
                    ft.Row([
                        ft.Icon(ft.Icons.PLAY_CIRCLE_OUTLINE, size=40),
                        ft.Column([
                            ft.Text(download.title, size=16, weight=ft.FontWeight.BOLD),
                            ft.Text(f"{download.platform} • {download.quality}", size=12),
                            ft.Text(status_text, size=12, color=status_color),
                        ], expand=True),
                        ft.Row(action_buttons)
                    ]),
                    progress_bar if download.status in ['downloading', 'paused'] else ft.Container()
                ])
            )
        )
    
    def pause_download(self, download_id: int):
        """Pause a download"""
        self.ytdlp_manager.pause_download(download_id)
        self.refresh_downloading_list()
        self.show_snackbar("Download paused")
    
    def resume_download(self, download_id: int):
        """Resume a download"""
        self.ytdlp_manager.resume_download(download_id)
        self.refresh_downloading_list()
        self.show_snackbar("Download resumed")
    
    def cancel_download(self, download_id: int):
        """Cancel a download"""
        self.ytdlp_manager.cancel_download(download_id)
        self.refresh_downloading_list()
        self.show_snackbar("Download cancelled")
    
    def retry_download(self, download_id: int):
        """Retry a failed download"""
        self.ytdlp_manager.retry_download(download_id, self.on_download_progress)
        self.refresh_downloading_list()
        self.show_snackbar("Download retrying...")
    
    def open_file(self, file_path: str):
        """Open downloaded file"""
        try:
            subprocess.run(['xdg-open', file_path])
        except Exception as e:
            self.show_error(f"Cannot open file: {str(e)}")
    
    def open_folder(self, file_path: str):
        """Open folder containing the file"""
        try:
            folder_path = os.path.dirname(file_path)
            subprocess.run(['xdg-open', folder_path])
        except Exception as e:
            self.show_error(f"Cannot open folder: {str(e)}")
    
    def download_again(self, url: str):
        """Download again from history"""
        self.url_input.value = url
        self.tabs.selected_index = 0  # Switch to add download tab
        self.analyze_url(None)
        self.page.update()
    
    def delete_from_history(self, download_id: int):
        """Delete download from history"""
        self.db_manager.delete_download(download_id)
        self.refresh_history_list()
        self.show_snackbar("Deleted from history")
    
    def browse_download_path(self, e):
        """Browse for download directory"""
        # For now, just show a dialog - in a real app, use file picker
        self.show_snackbar("Use file manager to choose folder and update the path manually")
    
    def save_settings(self, e):
        """Save settings to database"""
        settings = {
            'download_path': self.download_path_input.value,
            'preferred_quality': self.quality_dropdown.value,
            'preferred_format': self.format_dropdown_settings.value,
            'max_concurrent_downloads': self.max_downloads_input.value,
            'max_download_speed': self.max_speed_input.value,
            'clipboard_monitoring': str(self.clipboard_monitoring_switch.value).lower()
        }
        
        for key, value in settings.items():
            self.db_manager.set_setting(key, value)
        
        # Update manager settings
        self.ytdlp_manager.max_concurrent = int(settings['max_concurrent_downloads'])
        self.ytdlp_manager.max_speed = settings['max_download_speed']
        
        # Update clipboard monitoring
        if settings['clipboard_monitoring'] == 'true':
            self.start_clipboard_monitor()
        else:
            self.stop_clipboard_monitor()
        
        self.show_snackbar("Settings saved successfully!")
    
    def start_clipboard_monitor(self):
        """Start clipboard monitoring"""
        if self.db_manager.get_setting('clipboard_monitoring') == 'true':
            self.clipboard_monitor.start()
    
    def stop_clipboard_monitor(self):
        """Stop clipboard monitoring"""
        self.clipboard_monitor.stop()
    
    def on_clipboard_url(self, url: str):
        """Handle clipboard URL detection"""
        def show_clipboard_dialog():
            def download_from_clipboard(e):
                self.url_input.value = url
                self.tabs.selected_index = 0
                self.analyze_url(None)
                dialog.open = False
                self.page.update()
            
            def ignore_clipboard(e):
                dialog.open = False
                self.page.update()
            
            dialog = ft.AlertDialog(
                title=ft.Text("Video URL detected in clipboard"),
                content=ft.Text(f"Do you want to download:\n{url[:50]}..."),
                actions=[
                    ft.TextButton("Download", on_click=download_from_clipboard),
                    ft.TextButton("Ignore", on_click=ignore_clipboard)
                ]
            )
            
            self.page.dialog = dialog
            dialog.open = True
            self.page.update()
        
        self.page.run_thread(show_clipboard_dialog)
    
    def show_snackbar(self, message: str):
        """Show snackbar message"""
        self.page.snack_bar = ft.SnackBar(
            content=ft.Text(message),
            action="OK"
        )
        self.page.snack_bar.open = True
        self.page.update()
    
    def show_error(self, message: str):
        """Show error message"""
        self.page.snack_bar = ft.SnackBar(
            content=ft.Text(message),
            bgcolor=ft.Colors.RED,
            action="OK"
        )
        self.page.snack_bar.open = True
        self.page.update()
    
    def cleanup(self):
        """Cleanup resources"""
        self.stop_clipboard_monitor()
        
        # Cancel all active downloads
        for download_id in list(self.ytdlp_manager.active_downloads.keys()):
            self.ytdlp_manager.cancel_download(download_id)

def main(page: ft.Page):
    app = SnapTubeApp(page)
    
    # Handle app close
    def on_window_event(e):
        if e.data == "close":
            app.cleanup()
            page.window_destroy()
    
    page.window_on_event = on_window_event
    
    # Load initial data
    app.refresh_downloading_list()
    app.refresh_downloaded_list()
    app.refresh_history_list()

if __name__ == "__main__":
    # Check dependencies
    try:
        import flet
        import pyperclip
        import psutil
    except ImportError as e:
        print(f"Missing required package: {e}")
        print("Please install with: pip install flet pyperclip psutil")
        sys.exit(1)
    
    # Check yt-dlp
    try:
        subprocess.run(['yt-dlp', '--version'], capture_output=True, check=True)
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("yt-dlp not found. Please install with: pip install yt-dlp")
        sys.exit(1)
    
    # Run app
    ft.app(target=main, assets_dir="assets")
