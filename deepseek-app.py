from flask import Flask, render_template_string, request, jsonify, send_file
import json
import os
import logging
import qrcode
import io
import requests
import tempfile
import hashlib
import shutil
from datetime import datetime
from werkzeug.utils import secure_filename
from altparse import AltSourceManager, Parser, AltSource

# Configure logging
logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")

app = Flask(__name__)

# Configuration
SOURCE_FILE = "/app/data/source.json"
UPLOAD_FOLDER = "/app/data/uploads"
IPA_FOLDER = "/app/data/ipas"
ALLOWED_EXTENSIONS = {'ipa'}

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def get_file_size(filepath):
    """Get file size in bytes"""
    try:
        return os.path.getsize(filepath)
    except:
        return 0

class SourceManager:
    """Manages the AltSource data and file operations"""
    
    def __init__(self, source_file):
        self.source_file = source_file
        self.ensure_data_directory()
        self.initialize_source()
    
    def ensure_data_directory(self):
        """Ensure data and upload directories exist"""
        os.makedirs("/app/data", exist_ok=True)
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        os.makedirs(IPA_FOLDER, exist_ok=True)
        logging.info("Data directories verified")
    
    def get_ipa_path(self, bundle_id, version):
        """Get the file path for an IPA file"""
        # Create subdirectory for bundle ID
        bundle_folder = os.path.join(IPA_FOLDER, secure_filename(bundle_id))
        os.makedirs(bundle_folder, exist_ok=True)
        # Use version in filename
        filename = f"{secure_filename(version)}.ipa"
        return os.path.join(bundle_folder, filename)
    
    def save_ipa_file(self, file, bundle_id, version):
        """Save uploaded IPA file"""
        try:
            filepath = self.get_ipa_path(bundle_id, version)
            file.save(filepath)
            file_size = get_file_size(filepath)
            logging.info(f"Saved IPA file: {filepath} ({file_size} bytes)")
            return filepath, file_size
        except Exception as e:
            logging.error(f"Error saving IPA file: {str(e)}")
            return None, 0
    
    def download_ipa_from_url(self, url, bundle_id, version):
        """Download IPA file from URL and save it locally"""
        try:
            logging.info(f"Downloading IPA from: {url}")
            response = requests.get(url, stream=True, timeout=300)
            response.raise_for_status()
            
            filepath = self.get_ipa_path(bundle_id, version)
            with open(filepath, 'wb') as f:
                shutil.copyfileobj(response.raw, f)
            
            file_size = get_file_size(filepath)
            logging.info(f"Downloaded IPA file: {filepath} ({file_size} bytes)")
            return filepath, file_size
        except Exception as e:
            logging.error(f"Error downloading IPA file: {str(e)}")
            return None, 0
    
    def delete_ipa_file(self, bundle_id, version):
        """Delete IPA file for a version"""
        try:
            filepath = self.get_ipa_path(bundle_id, version)
            if os.path.exists(filepath):
                os.remove(filepath)
                logging.info(f"Deleted IPA file: {filepath}")
                # Try to remove bundle folder if empty
                bundle_folder = os.path.dirname(filepath)
                try:
                    if not os.listdir(bundle_folder):
                        os.rmdir(bundle_folder)
                except:
                    pass
                return True
            return False
        except Exception as e:
            logging.error(f"Error deleting IPA file: {str(e)}")
            return False
    
    def get_local_ipa_url(self, bundle_id, version, base_url=None):
        """Get the URL path for serving a local IPA file"""
        filename = f"{secure_filename(version)}.ipa"
        path = f"/ipas/{secure_filename(bundle_id)}/{filename}"
        if base_url:
            return f"{base_url.rstrip('/')}{path}"
        return path
    
    def initialize_source(self):
        """Initialize source.json with default data if it doesn't exist"""
        if not os.path.exists(self.source_file):
            initial_source = {
                "name": "My AltStore Source",
                "subtitle": "Custom iOS app repository",
                "description": "A custom source for managing iOS apps with AltStore and Feather",
                "iconURL": "https://f000.backblazeb2.com/file/rileytestut/ExampleSource/OctoSource.png",
                "headerURL": "https://f000.backblazeb2.com/file/rileytestut/ExampleSource/OceanHeader.png",
                "website": "https://example.com",
                "tintColor": "#4185A9",
                "featuredApps": [],
                "apps": [],
                "news": []
            }
            self.save_source(initial_source)
            logging.info("Initialized new source.json file")
    
    def load_source(self):
        """Load source data from JSON file"""
        try:
            with open(self.source_file, 'r') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"Error loading source: {str(e)}")
            return None
    
    def save_source(self, source_data):
        """Save source data to JSON file"""
        try:
            with open(self.source_file, 'w') as f:
                json.dump(source_data, f, indent=2)
            return True
        except Exception as e:
            logging.error(f"Error saving source: {str(e)}")
            return False
    
    def get_current_dates(self):
        """Get properly formatted dates for Feather compatibility"""
        current_date = datetime.now()
        return {
            'feather_date': current_date.strftime("%Y-%m-%d"),
            'version_date': current_date.strftime("%Y-%m-%dT%H:%M:%SZ")
        }
    
    def add_app_manual(self, data, ipa_file=None, download_from_url=False, base_url=None):
        """Add app manually with provided data"""
        source_data = self.load_source()
        if not source_data:
            return False, "Failed to load source data"
        
        dates = self.get_current_dates()
        bundle_id = data['bundleIdentifier']
        version = data['version']
        download_url = data.get('downloadURL', '')
        
        # Handle IPA file - upload, download, or use URL
        file_size = 0
        if ipa_file and allowed_file(ipa_file.filename):
            # Upload file
            filepath, file_size = self.save_ipa_file(ipa_file, bundle_id, version)
            if filepath:
                download_url = self.get_local_ipa_url(bundle_id, version, base_url)
        elif download_from_url and download_url:
            # Download from URL
            filepath, file_size = self.download_ipa_from_url(download_url, bundle_id, version)
            if filepath:
                download_url = self.get_local_ipa_url(bundle_id, version, base_url)
        
        new_app = {
            "name": data['name'],
            "bundleIdentifier": bundle_id,
            "developerName": data['developerName'],
            "localizedDescription": data.get('localizedDescription', ''),
            "iconURL": data.get('iconURL', ''),
            "addedDate": dates['feather_date'],
            "versions": [{
                "version": version,
                "date": dates['version_date'],
                "downloadURL": download_url,
                "minOSVersion": data.get('minOSVersion', '14.0'),
                "size": file_size
            }]
        }
        
        # Check if app already exists
        existing_index = None
        for i, app in enumerate(source_data['apps']):
            if app['bundleIdentifier'] == bundle_id:
                existing_index = i
                break
        
        if existing_index is not None:
            # Update existing app with new version
            source_data['apps'][existing_index]['versions'].insert(0, new_app['versions'][0])
            source_data['apps'][existing_index]['addedDate'] = dates['feather_date']
            logging.info(f"Updated app: {data['name']}")
        else:
            # Add new app
            source_data['apps'].append(new_app)
            logging.info(f"Added new app: {data['name']}")
        
        return self.save_source(source_data), "App added successfully"
    
    def get_app(self, bundle_identifier):
        """Get app by bundle identifier"""
        source_data = self.load_source()
        if not source_data:
            return None
        
        for app in source_data.get('apps', []):
            if app['bundleIdentifier'] == bundle_identifier:
                return app
        return None
    
    def update_app(self, bundle_identifier, data, ipa_file=None, download_from_url=False, base_url=None):
        """Update app details - supports updating download URLs without new versions"""
        source_data = self.load_source()
        if not source_data:
            return False, "Failed to load source data"
        
        app_index = None
        for i, app in enumerate(source_data['apps']):
            if app['bundleIdentifier'] == bundle_identifier:
                app_index = i
                break
        
        if app_index is None:
            return False, "App not found"
        
        # Update app fields
        app = source_data['apps'][app_index]
        updatable_fields = ['name', 'developerName', 'localizedDescription', 'iconURL']
        for field in updatable_fields:
            if field in data and data[field] is not None:
                app[field] = data[field]
        
        # Handle download URL update for the latest version
        if data.get('downloadURL') and app.get('versions'):
            # Update download URL for the latest version
            app['versions'][0]['downloadURL'] = data['downloadURL']
            
            # If we have an IPA file or download from URL, handle file operations
            if ipa_file and allowed_file(ipa_file.filename):
                version = app['versions'][0]['version']
                filepath, file_size = self.save_ipa_file(ipa_file, bundle_identifier, version)
                if filepath:
                    app['versions'][0]['downloadURL'] = self.get_local_ipa_url(bundle_identifier, version, base_url)
                    app['versions'][0]['size'] = file_size
            elif download_from_url and data.get('downloadURL'):
                version = app['versions'][0]['version']
                filepath, file_size = self.download_ipa_from_url(data['downloadURL'], bundle_identifier, version)
                if filepath:
                    app['versions'][0]['downloadURL'] = self.get_local_ipa_url(bundle_identifier, version, base_url)
                    app['versions'][0]['size'] = file_size
        
        success = self.save_source(source_data)
        return success, "App updated successfully" if success else "Failed to update app"
    
    def delete_app(self, bundle_identifier):
        """Delete app by bundle identifier"""
        source_data = self.load_source()
        if not source_data:
            return False, "Failed to load source data"
        
        # Find app to delete IPA files
        app_to_delete = None
        for app in source_data['apps']:
            if app['bundleIdentifier'] == bundle_identifier:
                app_to_delete = app
                break
        
        initial_count = len(source_data['apps'])
        source_data['apps'] = [
            app for app in source_data['apps'] 
            if app['bundleIdentifier'] != bundle_identifier
        ]
        
        if len(source_data['apps']) < initial_count:
            # Delete all IPA files for this app
            if app_to_delete:
                for version in app_to_delete.get('versions', []):
                    self.delete_ipa_file(bundle_identifier, version.get('version', ''))
            
            success = self.save_source(source_data)
            return success, "App deleted successfully" if success else "Failed to save source after deletion"
        else:
            return False, "App not found"
    
    def add_version(self, bundle_identifier, version_data, ipa_file=None, download_from_url=False, base_url=None):
        """Add a new version to an existing app"""
        source_data = self.load_source()
        if not source_data:
            return False, "Failed to load source data"
        
        app_index = None
        for i, app in enumerate(source_data['apps']):
            if app['bundleIdentifier'] == bundle_identifier:
                app_index = i
                break
        
        if app_index is None:
            return False, "App not found"
        
        dates = self.get_current_dates()
        app = source_data['apps'][app_index]
        
        # Ensure versions array exists
        if 'versions' not in app:
            app['versions'] = []
        
        # Get minOSVersion from provided data, or from existing version, or default
        min_os = version_data.get('minOSVersion')
        if not min_os and app['versions']:
            min_os = app['versions'][0].get('minOSVersion', '14.0')
        if not min_os:
            min_os = '14.0'
        
        version = version_data['version']
        download_url = version_data.get('downloadURL', '')
        file_size = version_data.get('size', 0)
        
        # Handle IPA file - upload, download, or use URL
        if ipa_file and allowed_file(ipa_file.filename):
            # Upload file
            filepath, file_size = self.save_ipa_file(ipa_file, bundle_identifier, version)
            if filepath:
                download_url = self.get_local_ipa_url(bundle_identifier, version, base_url)
        elif download_from_url and download_url:
            # Download from URL
            filepath, file_size = self.download_ipa_from_url(download_url, bundle_identifier, version)
            if filepath:
                download_url = self.get_local_ipa_url(bundle_identifier, version, base_url)
        
        new_version = {
            "version": version,
            "date": dates['version_date'],
            "downloadURL": download_url,
            "minOSVersion": min_os,
            "size": file_size
        }
        
        # Insert at the beginning (latest version first)
        app['versions'].insert(0, new_version)
        app['addedDate'] = dates['feather_date']
        
        success = self.save_source(source_data)
        return success, "Version added successfully" if success else "Failed to add version"
    
    def update_version(self, bundle_identifier, version, version_data):
        """Update a specific version of an app"""
        source_data = self.load_source()
        if not source_data:
            return False, "Failed to load source data"
        
        app_index = None
        for i, app in enumerate(source_data['apps']):
            if app['bundleIdentifier'] == bundle_identifier:
                app_index = i
                break
        
        if app_index is None:
            return False, "App not found"
        
        app = source_data['apps'][app_index]
        version_index = None
        
        for i, v in enumerate(app.get('versions', [])):
            if v['version'] == version:
                version_index = i
                break
        
        if version_index is None:
            return False, "Version not found"
        
        # Update version fields
        version_obj = app['versions'][version_index]
        updatable_fields = ['downloadURL', 'minOSVersion', 'size']
        for field in updatable_fields:
            if field in version_data and version_data[field] is not None:
                version_obj[field] = version_data[field]
        
        success = self.save_source(source_data)
        return success, "Version updated successfully" if success else "Failed to update version"
    
    def update_source_info(self, data):
        """Update source metadata"""
        source_data = self.load_source()
        if not source_data:
            return False, "Failed to load source data"
        
        for key in ['name', 'subtitle', 'description', 'website', 'tintColor']:
            if key in data and data[key]:
                source_data[key] = data[key]
        
        success = self.save_source(source_data)
        return success, "Source information updated successfully" if success else "Failed to update source information"

# Initialize source manager
source_manager = SourceManager(SOURCE_FILE)

HTML_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>AltStore Source Manager</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            padding: 20px;
        }
        .container {
            max-width: 1200px;
            margin: 0 auto;
            background: white;
            border-radius: 20px;
            padding: 40px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        }
        h1 { 
            color: #667eea;
            margin-bottom: 10px;
            font-size: 2.5em;
        }
        .subtitle {
            color: #666;
            margin-bottom: 30px;
            font-size: 1.1em;
        }
        .tabs {
            display: flex;
            gap: 10px;
            margin-bottom: 30px;
            border-bottom: 2px solid #eee;
            flex-wrap: wrap;
        }
        .tab {
            padding: 12px 24px;
            cursor: pointer;
            border: none;
            background: none;
            font-size: 16px;
            color: #666;
            border-bottom: 3px solid transparent;
            transition: all 0.3s;
            border-radius: 8px 8px 0 0;
        }
        .tab.active {
            color: #667eea;
            border-bottom-color: #667eea;
            font-weight: 600;
            background: #f8f9ff;
        }
        .tab:hover {
            color: #667eea;
            background: #f8f9ff;
        }
        .tab-content {
            display: none;
            animation: fadeIn 0.3s ease-in;
        }
        .tab-content.active {
            display: block;
        }
        .form-group {
            margin-bottom: 20px;
        }
        label {
            display: block;
            margin-bottom: 8px;
            color: #333;
            font-weight: 500;
        }
        input, textarea, select {
            width: 100%;
            padding: 12px;
            border: 2px solid #e0e0e0;
            border-radius: 8px;
            font-size: 14px;
            transition: border-color 0.3s;
        }
        input:focus, textarea:focus, select:focus {
            outline: none;
            border-color: #667eea;
            box-shadow: 0 0 0 3px rgba(102, 126, 234, 0.1);
        }
        textarea {
            min-height: 100px;
            font-family: inherit;
            resize: vertical;
        }
        .btn {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            padding: 14px 28px;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            font-size: 16px;
            font-weight: 600;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .btn:hover {
            transform: translateY(-2px);
            box-shadow: 0 10px 20px rgba(102, 126, 234, 0.3);
        }
        .btn:active {
            transform: translateY(0);
        }
        .btn-danger {
            background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
        }
        .btn-danger:hover {
            box-shadow: 0 10px 20px rgba(245, 87, 108, 0.3);
        }
        .app-list {
            display: grid;
            gap: 20px;
            margin-top: 20px;
        }
        .app-card {
            border: 2px solid #e0e0e0;
            border-radius: 12px;
            padding: 20px;
            transition: all 0.3s;
            background: #fafafa;
        }
        .app-card:hover {
            border-color: #667eea;
            box-shadow: 0 5px 15px rgba(102, 126, 234, 0.2);
            transform: translateY(-2px);
        }
        .app-header {
            display: flex;
            justify-content: space-between;
            align-items: start;
            margin-bottom: 15px;
            gap: 15px;
        }
        .app-info {
            flex: 1;
        }
        .app-info h3 {
            color: #333;
            margin-bottom: 5px;
            font-size: 1.2em;
        }
        .app-info p {
            color: #666;
            font-size: 0.9em;
            margin-bottom: 5px;
        }
        .qr-container {
            text-align: center;
            padding: 30px;
        }
        .qr-container img {
            max-width: 300px;
            border: 5px solid #667eea;
            border-radius: 12px;
            padding: 10px;
            background: white;
            margin: 0 auto 20px;
        }
        .source-url {
            background: #f5f5f5;
            padding: 15px;
            border-radius: 8px;
            margin: 15px 0;
            font-family: 'SF Mono', 'Monaco', 'Inconsolata', 'Roboto Mono', monospace;
            word-break: break-all;
            border: 1px solid #e0e0e0;
        }
        .alert {
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 20px;
        }
        .alert-success {
            background: #d4edda;
            color: #155724;
            border: 1px solid #c3e6cb;
        }
        .alert-error {
            background: #f8d7da;
            color: #721c24;
            border: 1px solid #f5c6cb;
        }
        .version-info {
            font-size: 0.85em;
            color: #666;
            margin-top: 5px;
            line-height: 1.4;
        }
        .info-box {
            background: #e8f4fd;
            padding: 15px;
            border-radius: 8px;
            border-left: 4px solid #667eea;
            margin: 20px 0;
        }
        .btn-secondary {
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            margin-right: 10px;
        }
        .btn-group {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }
        .modal {
            display: none;
            position: fixed;
            z-index: 1000;
            left: 0;
            top: 0;
            width: 100%;
            height: 100%;
            overflow: auto;
            background-color: rgba(0,0,0,0.5);
            animation: fadeIn 0.3s;
        }
        .modal-content {
            background-color: white;
            margin: 5% auto;
            padding: 30px;
            border-radius: 20px;
            width: 90%;
            max-width: 600px;
            max-height: 90vh;
            overflow-y: auto;
            box-shadow: 0 20px 60px rgba(0,0,0,0.3);
        }
        .modal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 2px solid #eee;
        }
        .modal-header h2 {
            margin: 0;
            color: #667eea;
        }
        .close {
            color: #aaa;
            font-size: 28px;
            font-weight: bold;
            cursor: pointer;
            border: none;
            background: none;
        }
        .close:hover {
            color: #667eea;
        }
        .version-list {
            margin-top: 20px;
            padding-top: 20px;
            border-top: 2px solid #eee;
        }
        .version-item {
            background: #f8f9fa;
            padding: 15px;
            border-radius: 8px;
            margin-bottom: 10px;
            border-left: 4px solid #667eea;
        }
        .version-item-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
        }
        .version-item-header strong {
            color: #333;
            font-size: 1.1em;
        }
        .version-item-details {
            font-size: 0.9em;
            color: #666;
            margin-top: 5px;
        }
        .add-version-section {
            background: #f8f9ff;
            padding: 20px;
            border-radius: 8px;
            margin-top: 20px;
            border: 2px dashed #667eea;
        }
        .app-icon {
            width: 60px;
            height: 60px;
            border-radius: 12px;
            object-fit: cover;
            margin-right: 15px;
            border: 2px solid #e0e0e0;
            flex-shrink: 0;
        }
        .app-header-with-icon {
            display: flex;
            align-items: start;
        }
        .search-filter-bar {
            display: flex;
            gap: 15px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        .search-box {
            flex: 1;
            min-width: 200px;
            position: relative;
        }
        .search-box input {
            padding-left: 40px;
        }
        .search-icon {
            position: absolute;
            left: 12px;
            top: 50%;
            transform: translateY(-50%);
            color: #999;
            font-size: 18px;
        }
        .filter-select {
            min-width: 150px;
        }
        .toast-container {
            position: fixed;
            top: 20px;
            right: 20px;
            z-index: 10000;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .toast {
            background: white;
            padding: 16px 20px;
            border-radius: 8px;
            box-shadow: 0 4px 12px rgba(0,0,0,0.15);
            display: flex;
            align-items: center;
            gap: 12px;
            min-width: 300px;
            max-width: 500px;
            animation: slideInRight 0.3s ease-out;
            border-left: 4px solid #667eea;
        }
        .toast.success {
            border-left-color: #10b981;
        }
        .toast.error {
            border-left-color: #ef4444;
        }
        .toast.warning {
            border-left-color: #f59e0b;
        }
        .toast-icon {
            font-size: 20px;
            flex-shrink: 0;
        }
        .toast-content {
            flex: 1;
        }
        .toast-title {
            font-weight: 600;
            margin-bottom: 4px;
            color: #333;
        }
        .toast-message {
            font-size: 14px;
            color: #666;
        }
        .toast-close {
            background: none;
            border: none;
            font-size: 20px;
            color: #999;
            cursor: pointer;
            padding: 0;
            width: 24px;
            height: 24px;
            display: flex;
            align-items: center;
            justify-content: center;
        }
        .toast-close:hover {
            color: #333;
        }
        .spinner {
            border: 3px solid #f3f3f3;
            border-top: 3px solid #667eea;
            border-radius: 50%;
            width: 24px;
            height: 24px;
            animation: spin 1s linear infinite;
            display: inline-block;
            margin-right: 8px;
        }
        .btn:disabled {
            opacity: 0.6;
            cursor: not-allowed;
            transform: none;
        }
        .btn.loading {
            position: relative;
            color: transparent;
        }
        .btn.loading::after {
            content: '';
            position: absolute;
            width: 20px;
            height: 20px;
            top: 50%;
            left: 50%;
            margin-left: -10px;
            margin-top: -10px;
            border: 2px solid #ffffff;
            border-top-color: transparent;
            border-radius: 50%;
            animation: spin 0.8s linear infinite;
        }
        .loading-overlay {
            position: absolute;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(255, 255, 255, 0.9);
            display: flex;
            align-items: center;
            justify-content: center;
            border-radius: 12px;
            z-index: 10;
        }
        @keyframes fadeIn {
            from { opacity: 0; transform: translateY(10px); }
            to { opacity: 1; transform: translateY(0); }
        }
        @keyframes slideInRight {
            from { 
                opacity: 0;
                transform: translateX(100%);
            }
            to {
                opacity: 1;
                transform: translateX(0);
            }
        }
        @keyframes spin {
            0% { transform: rotate(0deg); }
            100% { transform: rotate(360deg); }
        }
        @media (max-width: 768px) {
            .container {
                padding: 20px;
                margin: 10px;
            }
            .app-header {
                flex-direction: column;
            }
            .tabs {
                flex-direction: column;
            }
            .tab {
                text-align: center;
            }
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🚀 AltStore Source Manager</h1>
        <p class="subtitle">Manage your custom iOS app repository</p>
        
        <div class="tabs">
            <button class="tab active" onclick="switchTab('add-app', this)">Add App</button>
            <button class="tab" onclick="switchTab('manage-apps', this)">Manage Apps</button>
            <button class="tab" onclick="switchTab('qr-code', this)">QR Code</button>
            <button class="tab" onclick="switchTab('source-info', this)">Source Info</button>
        </div>

        <div id="add-app" class="tab-content active">
            <h2 style="margin-bottom: 20px;">➕ Add New App</h2>
            <form id="addAppForm">
                <div id="manualFields">
                    <div class="form-group">
                        <label>App Name:</label>
                        <input type="text" name="name" required placeholder="My Awesome App">
                    </div>
                    <div class="form-group">
                        <label>Bundle Identifier:</label>
                        <input type="text" name="bundleIdentifier" placeholder="com.example.app" required>
                    </div>
                    <div class="form-group">
                        <label>Developer Name:</label>
                        <input type="text" name="developerName" required placeholder="Developer Name">
                    </div>
                    <div class="form-group">
                        <label>Description:</label>
                        <textarea name="localizedDescription" placeholder="App description..."></textarea>
                    </div>
                    <div class="form-group">
                        <label>Icon URL:</label>
                        <input type="url" name="iconURL" placeholder="https://example.com/icon.png">
                    </div>
                    <div class="form-group">
                        <label>Version:</label>
                        <input type="text" name="version" placeholder="1.0.0" required>
                    </div>
                    <div class="form-group">
                        <label>IPA File:</label>
                        <div style="margin-bottom: 10px;">
                            <input type="file" id="ipaFile" name="ipaFile" accept=".ipa" style="margin-bottom: 10px; width: 100%;">
                            <div style="display: flex; align-items: center; gap: 8px; margin-top: 10px;">
                                <input type="checkbox" id="downloadFromUrl" name="downloadFromUrl" value="true" onchange="toggleDownloadMethod()" style="width: auto; margin: 0; cursor: pointer;">
                                <label for="downloadFromUrl" style="margin: 0; font-weight: normal; cursor: pointer; user-select: none;">Use URL instead</label>
                            </div>
                        </div>
                        <input type="url" id="downloadURL" name="downloadURL" placeholder="https://example.com/app.ipa" style="display: none; width: 100%;">
                    </div>
                    <div class="form-group">
                        <label>Min iOS Version:</label>
                        <input type="text" name="minOSVersion" placeholder="14.0">
                    </div>
                </div>

                <button type="submit" class="btn">Add App</button>
            </form>
        </div>

        <div id="manage-apps" class="tab-content">
            <h2 style="margin-bottom: 20px;">📱 Installed Apps</h2>
            <div class="search-filter-bar">
                <div class="search-box">
                    <span class="search-icon">🔍</span>
                    <input type="text" id="searchInput" placeholder="Search apps by name or bundle ID..." oninput="filterApps()">
                </div>
                <select id="filterDeveloper" class="filter-select" onchange="filterApps()">
                    <option value="">All Developers</option>
                </select>
                <select id="sortBy" class="filter-select" onchange="filterApps()">
                    <option value="name">Sort by Name</option>
                    <option value="date">Sort by Date</option>
                    <option value="version">Sort by Version</option>
                </select>
            </div>
            <div id="appsList" class="app-list" style="position: relative;">
                <div id="loadingOverlay" class="loading-overlay" style="display: none;">
                    <div class="spinner"></div>
                </div>
                <p style="color: #666; text-align: center; padding: 40px;">Loading apps...</p>
            </div>
        </div>

        <!-- Edit App Modal -->
        <div id="editAppModal" class="modal">
            <div class="modal-content">
                <div class="modal-header">
                    <h2>✏️ Edit App</h2>
                    <button class="close" onclick="closeEditModal()">&times;</button>
                </div>
                <form id="editAppForm">
                    <input type="hidden" id="editBundleId" name="bundleIdentifier">
                    <div class="form-group" style="text-align: center; margin-bottom: 20px;">
                        <img id="editAppIcon" src="" alt="App Icon" class="app-icon" style="margin: 0 auto; display: block;">
                    </div>
                    <div class="form-group">
                        <label>App Name:</label>
                        <input type="text" id="editName" name="name" required>
                    </div>
                    <div class="form-group">
                        <label>Developer Name:</label>
                        <input type="text" id="editDeveloperName" name="developerName" required>
                    </div>
                    <div class="form-group">
                        <label>Description:</label>
                        <textarea id="editDescription" name="localizedDescription" rows="4"></textarea>
                    </div>
                    <div class="form-group">
                        <label>Icon URL:</label>
                        <input type="url" id="editIconURL" name="iconURL">
                    </div>
                    <div class="form-group">
                        <label>Update Latest Version Download URL:</label>
                        <input type="url" id="editDownloadURL" name="downloadURL" placeholder="https://example.com/app.ipa">
                        <div style="display: flex; align-items: center; gap: 8px; margin-top: 10px;">
                            <input type="checkbox" id="editDownloadFromUrl" name="downloadFromUrl" value="true" style="width: auto; margin: 0; cursor: pointer;">
                            <label for="editDownloadFromUrl" style="margin: 0; font-weight: normal; cursor: pointer; user-select: none;">Download and host locally</label>
                        </div>
                    </div>
                    <div class="form-group">
                        <label>Bundle Identifier (read-only):</label>
                        <input type="text" id="editBundleIdentifier" readonly style="background: #f5f5f5;">
                    </div>
                    
                    <div class="version-list">
                        <h3 style="margin-bottom: 15px;">📦 Versions</h3>
                        <div id="versionsList"></div>
                        
                        <div class="add-version-section">
                            <h4 style="margin-bottom: 15px; color: #667eea;">➕ Add New Version</h4>
                            <div class="form-group">
                                <label>Version:</label>
                                <input type="text" id="newVersion" name="version" placeholder="1.0.0" required>
                            </div>
                            <div class="form-group">
                                <label>IPA File:</label>
                                <div style="margin-bottom: 10px;">
                                    <input type="file" id="newIpaFile" name="ipaFile" accept=".ipa" style="margin-bottom: 10px; width: 100%;">
                                    <div style="display: flex; align-items: center; gap: 8px; margin-top: 10px;">
                                        <input type="checkbox" id="newDownloadFromUrl" name="downloadFromUrl" value="true" onchange="toggleNewVersionDownloadMethod()" style="width: auto; margin: 0; cursor: pointer;">
                                        <label for="newDownloadFromUrl" style="margin: 0; font-weight: normal; cursor: pointer; user-select: none;">Use URL instead</label>
                                    </div>
                                </div>
                                <input type="url" id="newDownloadURL" name="downloadURL" placeholder="https://example.com/app.ipa" style="display: none; width: 100%;">
                            </div>
                            <div class="form-group">
                                <label>Min iOS Version:</label>
                                <input type="text" id="newMinOSVersion" name="minOSVersion" placeholder="14.0">
                            </div>
                            <button type="button" class="btn" id="addVersionBtn" onclick="addNewVersion(this)">Add Version</button>
                        </div>
                    </div>
                    
                    <div class="btn-group" style="margin-top: 20px;">
                        <button type="submit" class="btn">Save Changes</button>
                        <button type="button" class="btn btn-danger" onclick="closeEditModal()">Cancel</button>
                    </div>
                </form>
            </div>
        </div>

        <div id="qr-code" class="tab-content">
            <div class="qr-container">
                <h2 style="margin-bottom: 20px;">📱 Add to Feather</h2>
                <p style="margin-bottom: 20px;">Scan this QR code with Feather to instantly add this source:</p>
                <img id="qrImage" src="/qr" alt="QR Code">
                <div class="source-url">
                    <strong>Feather URL:</strong><br>
                    <span id="featherUrl"></span>
                </div>
                <div class="info-box">
                    <strong>💡 Pro Tip:</strong> The QR code uses the <code>feather://</code> URL scheme for one-tap source addition in the Feather app.
                </div>
                <div class="source-url">
                    <strong>Alternative URL (for AltStore):</strong><br>
                    <span id="sourceUrl"></span>
                </div>
            </div>
        </div>

        <div id="source-info" class="tab-content">
            <h2 style="margin-bottom: 20px;">⚙️ Source Information</h2>
            <form id="sourceInfoForm">
                <div class="form-group">
                    <label>Source Name:</label>
                    <input type="text" name="name" id="sourceName">
                </div>
                <div class="form-group">
                    <label>Subtitle:</label>
                    <input type="text" name="subtitle" id="sourceSubtitle">
                </div>
                <div class="form-group">
                    <label>Description:</label>
                    <textarea name="description" id="sourceDescription"></textarea>
                </div>
                <div class="form-group">
                    <label>Website:</label>
                    <input type="url" name="website" id="sourceWebsite">
                </div>
                <div class="form-group">
                    <label>Tint Color (hex):</label>
                    <input type="text" name="tintColor" id="sourceTintColor" placeholder="#4185A9">
                </div>
                <button type="submit" class="btn">Update Source Info</button>
            </form>
        </div>
    </div>

    <!-- Toast Container -->
    <div id="toastContainer" class="toast-container"></div>

    <script>
        let currentHost = window.location.origin;
        let sourceUrl = currentHost + '/source.json';
        let featherUrl = sourceUrl.replace('https://', 'feather://').replace('http://', 'feather://');
        let allApps = []; // Store all apps for filtering

        document.getElementById('sourceUrl').textContent = sourceUrl;
        document.getElementById('featherUrl').textContent = featherUrl;

        // Toast Notification System
        function showToast(message, type = 'info', title = '') {
            const container = document.getElementById('toastContainer');
            const toast = document.createElement('div');
            toast.className = `toast ${type}`;
            
            const icons = {
                success: '✅',
                error: '❌',
                warning: '⚠️',
                info: 'ℹ️'
            };
            
            const titles = {
                success: 'Success',
                error: 'Error',
                warning: 'Warning',
                info: 'Info'
            };
            
            toast.innerHTML = `
                <span class="toast-icon">${icons[type] || icons.info}</span>
                <div class="toast-content">
                    ${title ? `<div class="toast-title">${title}</div>` : ''}
                    <div class="toast-message">${escapeHtml(message)}</div>
                </div>
                <button class="toast-close" onclick="this.parentElement.remove()">&times;</button>
            `;
            
            container.appendChild(toast);
            
            // Auto remove after 5 seconds
            setTimeout(() => {
                if (toast.parentElement) {
                    toast.style.animation = 'slideInRight 0.3s ease-out reverse';
                    setTimeout(() => toast.remove(), 300);
                }
            }, 5000);
        }

        // Loading state helpers
        function setButtonLoading(button, loading) {
            if (loading) {
                button.disabled = true;
                button.classList.add('loading');
            } else {
                button.disabled = false;
                button.classList.remove('loading');
            }
        }

        function setLoadingOverlay(show) {
            const overlay = document.getElementById('loadingOverlay');
            if (overlay) {
                overlay.style.display = show ? 'flex' : 'none';
            }
        }

        function switchTab(tabName, clickedTab) {
            document.querySelectorAll('.tab').forEach(tab => tab.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(content => content.classList.remove('active'));
            
            if (clickedTab) {
                clickedTab.classList.add('active');
            }
            document.getElementById(tabName).classList.add('active');

            if (tabName === 'manage-apps') {
                loadApps();
            } else if (tabName === 'source-info') {
                loadSourceInfo();
            }
        }

        function toggleDownloadMethod() {
            const checkbox = document.getElementById('downloadFromUrl');
            const fileInput = document.getElementById('ipaFile');
            const urlInput = document.getElementById('downloadURL');
            
            if (checkbox.checked) {
                fileInput.style.display = 'none';
                urlInput.style.display = 'block';
                urlInput.required = true;
                fileInput.required = false;
            } else {
                fileInput.style.display = 'block';
                urlInput.style.display = 'none';
                urlInput.required = false;
                fileInput.required = true;
            }
        }

        function toggleNewVersionDownloadMethod() {
            const checkbox = document.getElementById('newDownloadFromUrl');
            const fileInput = document.getElementById('newIpaFile');
            const urlInput = document.getElementById('newDownloadURL');
            
            if (checkbox.checked) {
                fileInput.style.display = 'none';
                urlInput.style.display = 'block';
                urlInput.required = true;
                fileInput.required = false;
            } else {
                fileInput.style.display = 'block';
                urlInput.style.display = 'none';
                urlInput.required = false;
                fileInput.required = true;
            }
        }

        document.getElementById('addAppForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const submitBtn = e.target.querySelector('button[type="submit"]');
            setButtonLoading(submitBtn, true);
            
            const formData = new FormData(e.target);
            const ipaFile = document.getElementById('ipaFile').files[0];
            const useUrl = document.getElementById('downloadFromUrl').checked;
            
            // If file is selected, add it to formData
            if (ipaFile && !useUrl) {
                formData.append('ipaFile', ipaFile);
            }
            if (useUrl) {
                formData.append('downloadFromUrl', 'true');
            }

            try {
                const response = await fetch('/api/add-app', {
                    method: 'POST',
                    body: formData
                });

                const result = await response.json();
                if (result.success) {
                    showToast(result.message || 'App added successfully!', 'success');
                    e.target.reset();
                    toggleDownloadMethod(); // Reset UI
                } else {
                    showToast(result.error || 'Unknown error occurred', 'error');
                }
            } catch (error) {
                showToast('Error adding app: ' + error.message, 'error');
            } finally {
                setButtonLoading(submitBtn, false);
            }
        });

        async function loadApps() {
            try {
                setLoadingOverlay(true);
                const appsList = document.getElementById('appsList');
                const loadingMsg = appsList.querySelector('p');
                if (loadingMsg) loadingMsg.style.display = 'block';
                
                const response = await fetch('/api/apps');
                const apps = await response.json();
                
                allApps = apps; // Store for filtering
                
                // Populate developer filter
                const developerSelect = document.getElementById('filterDeveloper');
                if (developerSelect) {
                    const developers = [...new Set(apps.map(app => app.developerName || 'Unknown').filter(Boolean))].sort();
                    const currentValue = developerSelect.value;
                    developerSelect.innerHTML = '<option value="">All Developers</option>' + 
                        developers.map(dev => `<option value="${escapeHtml(dev)}">${escapeHtml(dev)}</option>`).join('');
                    developerSelect.value = currentValue;
                }
                
                if (apps.length === 0) {
                    appsList.innerHTML = '<p style="color: #666; text-align: center; padding: 40px;">No apps added yet. Go to the "Add App" tab to add your first app! 📱</p>';
                    setLoadingOverlay(false);
                    return;
                }

                renderApps(apps);
                setLoadingOverlay(false);
            } catch (error) {
                console.error('Error loading apps:', error);
                showToast('Failed to load apps. Please try again.', 'error');
                document.getElementById('appsList').innerHTML = '<p style="color: #f5576c; text-align: center; padding: 20px;">Error loading apps. Please try again.</p>';
                setLoadingOverlay(false);
            }
        }

        function renderApps(apps) {
            const appsList = document.getElementById('appsList');
            const loadingMsg = appsList.querySelector('p');
            if (loadingMsg) loadingMsg.style.display = 'none';
            
            if (apps.length === 0) {
                appsList.innerHTML = '<p style="color: #666; text-align: center; padding: 40px;">No apps found matching your search.</p>';
                return;
            }

            appsList.innerHTML = apps.map(app => {
                const iconUrl = app.iconURL || 'https://via.placeholder.com/60/667eea/ffffff?text=' + encodeURIComponent((app.name || 'App').charAt(0));
                return `
                    <div class="app-card">
                        <div class="app-header">
                            <div class="app-header-with-icon">
                                <img src="${escapeHtml(iconUrl)}" alt="${escapeHtml(app.name)}" class="app-icon" onerror="this.src='https://via.placeholder.com/60/667eea/ffffff?text=${encodeURIComponent((app.name || 'App').charAt(0))}'">
                                <div class="app-info">
                                    <h3>${escapeHtml(app.name)}</h3>
                                    <p><strong>Bundle ID:</strong> ${escapeHtml(app.bundleIdentifier)}</p>
                                    <div class="version-info">
                                        <strong>Latest:</strong> ${app.versions?.[0]?.version || 'N/A'} | 
                                        <strong>Developer:</strong> ${escapeHtml(app.developerName || 'Unknown')} |
                                        <strong>Added:</strong> ${app.addedDate ? new Date(app.addedDate).toLocaleDateString() : 'N/A'} |
                                        <strong>Versions:</strong> ${app.versions?.length || 0}
                                    </div>
                                </div>
                            </div>
                            <div class="btn-group">
                                <button class="btn btn-secondary" onclick="editApp('${escapeHtml(app.bundleIdentifier)}')">Edit</button>
                                <button class="btn btn-danger" onclick="deleteApp('${escapeHtml(app.bundleIdentifier)}')">Delete</button>
                            </div>
                        </div>
                        <p>${escapeHtml(app.localizedDescription?.substring(0, 150) || 'No description available')}${app.localizedDescription?.length > 150 ? '...' : ''}</p>
                    </div>
                `;
            }).join('');
        }

        function filterApps() {
            const searchTerm = (document.getElementById('searchInput')?.value || '').toLowerCase();
            const developerFilter = document.getElementById('filterDeveloper')?.value || '';
            const sortBy = document.getElementById('sortBy')?.value || 'name';
            
            let filtered = [...allApps];
            
            // Search filter
            if (searchTerm) {
                filtered = filtered.filter(app => 
                    app.name.toLowerCase().includes(searchTerm) ||
                    app.bundleIdentifier.toLowerCase().includes(searchTerm) ||
                    (app.developerName || '').toLowerCase().includes(searchTerm)
                );
            }
            
            // Developer filter
            if (developerFilter) {
                filtered = filtered.filter(app => (app.developerName || 'Unknown') === developerFilter);
            }
            
            // Sort
            filtered.sort((a, b) => {
                switch(sortBy) {
                    case 'name':
                        return (a.name || '').localeCompare(b.name || '');
                    case 'date':
                        const dateA = a.addedDate || '';
                        const dateB = b.addedDate || '';
                        return dateB.localeCompare(dateA); // Newest first
                    case 'version':
                        const verA = a.versions?.[0]?.version || '';
                        const verB = b.versions?.[0]?.version || '';
                        return verB.localeCompare(verA); // Newest version first
                    default:
                        return 0;
                }
            });
            
            renderApps(filtered);
        }

        function escapeHtml(unsafe) {
            if (!unsafe) return '';
            return unsafe
                .replace(/&/g, "&amp;")
                .replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
                .replace(/"/g, "&quot;")
                .replace(/'/g, "&#039;");
        }

        async function editApp(bundleId) {
            try {
                const response = await fetch(`/api/app/${encodeURIComponent(bundleId)}`);
                const app = await response.json();
                
                if (app.error) {
                    showToast(app.error, 'error');
                    return;
                }
                
                // Populate form fields
                document.getElementById('editBundleId').value = app.bundleIdentifier;
                document.getElementById('editBundleIdentifier').value = app.bundleIdentifier;
                document.getElementById('editName').value = app.name || '';
                document.getElementById('editDeveloperName').value = app.developerName || '';
                document.getElementById('editDescription').value = app.localizedDescription || '';
                document.getElementById('editIconURL').value = app.iconURL || '';
                
                // Populate latest version download URL
                const latestVersion = app.versions?.[0];
                if (latestVersion) {
                    document.getElementById('editDownloadURL').value = latestVersion.downloadURL || '';
                }
                
                // Set app icon
                const iconImg = document.getElementById('editAppIcon');
                const iconUrl = app.iconURL || 'https://via.placeholder.com/60/667eea/ffffff?text=' + encodeURIComponent((app.name || 'App').charAt(0));
                iconImg.src = iconUrl;
                iconImg.onerror = function() {
                    this.src = 'https://via.placeholder.com/60/667eea/ffffff?text=' + encodeURIComponent((app.name || 'App').charAt(0));
                };
                
                // Display versions
                displayVersions(app.versions || []);
                
                // Show modal
                document.getElementById('editAppModal').style.display = 'block';
            } catch (error) {
                showToast('Error loading app: ' + error.message, 'error');
            }
        }
        
        function displayVersions(versions) {
            const versionsList = document.getElementById('versionsList');
            if (!versions || versions.length === 0) {
                versionsList.innerHTML = '<p style="color: #666; text-align: center; padding: 20px;">No versions available</p>';
                return;
            }
            
            versionsList.innerHTML = versions.map((version, index) => {
                const versionId = `version-${index}`;
                return `
                <div class="version-item" id="${versionId}">
                    <div class="version-item-header">
                        <strong>Version ${escapeHtml(version.version)}</strong>
                        ${index === 0 ? '<span style="background: #667eea; color: white; padding: 4px 8px; border-radius: 4px; font-size: 0.8em;">Latest</span>' : ''}
                    </div>
                    <div class="version-item-details">
                        <div><strong>Date:</strong> ${version.date ? new Date(version.date).toLocaleString() : 'N/A'}</div>
                        <div><strong>Download URL:</strong> ${escapeHtml(version.downloadURL || 'N/A')}</div>
                        <div><strong>Min iOS:</strong> ${escapeHtml(version.minOSVersion || 'N/A')}</div>
                        <div><strong>Size:</strong> ${version.size ? (version.size / 1024 / 1024).toFixed(2) + ' MB' : 'N/A'}</div>
                    </div>
                </div>
            `;
            }).join('');
        }
        
        function closeEditModal() {
            document.getElementById('editAppModal').style.display = 'none';
            document.getElementById('editAppForm').reset();
            document.getElementById('newVersion').value = '';
            document.getElementById('newDownloadURL').value = '';
            document.getElementById('newMinOSVersion').value = '';
        }
        
        async function addNewVersion(button) {
            const bundleId = document.getElementById('editBundleId').value;
            const version = document.getElementById('newVersion').value;
            const minOSVersion = document.getElementById('newMinOSVersion').value;
            const ipaFile = document.getElementById('newIpaFile')?.files[0];
            const useUrl = document.getElementById('newDownloadFromUrl')?.checked || false;
            const downloadURL = document.getElementById('newDownloadURL')?.value || '';
            
            if (!version) {
                showToast('Version is required', 'error');
                return;
            }
            
            if (!ipaFile && !useUrl && !downloadURL) {
                showToast('Either IPA file or Download URL is required', 'error');
                return;
            }
            
            if (button) setButtonLoading(button, true);
            
            try {
                const formData = new FormData();
                formData.append('bundleIdentifier', bundleId);
                formData.append('version', version);
                formData.append('minOSVersion', minOSVersion || '');
                
                if (ipaFile && !useUrl) {
                    formData.append('ipaFile', ipaFile);
                } else if (useUrl && downloadURL) {
                    formData.append('downloadURL', downloadURL);
                    formData.append('downloadFromUrl', 'true');
                } else if (downloadURL) {
                    formData.append('downloadURL', downloadURL);
                }
                
                const response = await fetch('/api/add-version', {
                    method: 'POST',
                    body: formData
                });
                
                const result = await response.json();
                if (result.success) {
                    showToast(result.message || 'Version added successfully!', 'success');
                    // Reload app data to show new version
                    await editApp(bundleId);
                    // Clear form
                    document.getElementById('newVersion').value = '';
                    document.getElementById('newDownloadURL').value = '';
                    document.getElementById('newMinOSVersion').value = '';
                    if (document.getElementById('newIpaFile')) {
                        document.getElementById('newIpaFile').value = '';
                    }
                    toggleNewVersionDownloadMethod(); // Reset UI
                } else {
                    showToast(result.error || 'Failed to add version', 'error');
                }
            } catch (error) {
                showToast('Error adding version: ' + error.message, 'error');
            } finally {
                if (button) setButtonLoading(button, false);
            }
        }
        
        document.getElementById('editAppForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const submitBtn = e.target.querySelector('button[type="submit"]');
            setButtonLoading(submitBtn, true);
            
            const formData = new FormData(e.target);
            const ipaFile = document.getElementById('newIpaFile')?.files[0];
            const useUrl = document.getElementById('editDownloadFromUrl')?.checked || false;
            
            // Remove version-specific fields from the main form data
            formData.delete('version');
            formData.delete('minOSVersion');
            
            // Add IPA file if provided for download URL update
            if (ipaFile && !useUrl) {
                formData.append('ipaFile', ipaFile);
            }
            if (useUrl) {
                formData.append('downloadFromUrl', 'true');
            }
            
            try {
                const response = await fetch('/api/update-app', {
                    method: 'POST',
                    body: formData
                });
                
                const result = await response.json();
                if (result.success) {
                    showToast(result.message || 'App updated successfully!', 'success');
                    closeEditModal();
                    loadApps();
                } else {
                    showToast(result.error || 'Failed to update app', 'error');
                }
            } catch (error) {
                showToast('Error updating app: ' + error.message, 'error');
            } finally {
                setButtonLoading(submitBtn, false);
            }
        });
        
        // Close modal when clicking outside
        window.onclick = function(event) {
            const modal = document.getElementById('editAppModal');
            if (event.target == modal) {
                closeEditModal();
            }
        }

        async function deleteApp(bundleId) {
            if (!confirm('Are you sure you want to delete this app? This action cannot be undone.')) return;

            try {
                const response = await fetch('/api/delete-app', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ bundleIdentifier: bundleId })
                });

                const result = await response.json();
                if (result.success) {
                    showToast(result.message || 'App deleted successfully!', 'success');
                    loadApps();
                } else {
                    showToast(result.error || 'Failed to delete app', 'error');
                }
            } catch (error) {
                showToast('Error deleting app: ' + error.message, 'error');
            }
        }

        async function loadSourceInfo() {
            try {
                const response = await fetch('/source.json');
                const source = await response.json();
                
                document.getElementById('sourceName').value = source.name || '';
                document.getElementById('sourceSubtitle').value = source.subtitle || '';
                document.getElementById('sourceDescription').value = source.description || '';
                document.getElementById('sourceWebsite').value = source.website || '';
                document.getElementById('sourceTintColor').value = source.tintColor || '';
            } catch (error) {
                console.error('Error loading source info:', error);
            }
        }

        document.getElementById('sourceInfoForm').addEventListener('submit', async (e) => {
            e.preventDefault();
            const formData = new FormData(e.target);
            const data = Object.fromEntries(formData.entries());

            try {
                const response = await fetch('/api/update-source', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(data)
                });

                const result = await response.json();
                if (result.success) {
                    showToast(result.message || 'Source information updated successfully!', 'success');
                } else {
                    showToast(result.error || 'Failed to update source information', 'error');
                }
            } catch (error) {
                showToast('Error updating source info: ' + error.message, 'error');
            }
        });

        // Load apps on initial page load if on manage apps tab
        document.addEventListener('DOMContentLoaded', function() {
            if (document.getElementById('manage-apps').classList.contains('active')) {
                loadApps();
            }
        });
    </script>
</body>
</html>
'''

# Routes
@app.route('/')
def index():
    return render_template_string(HTML_TEMPLATE)

@app.route('/source.json')
def serve_source():
    try:
        return send_file(SOURCE_FILE, mimetype='application/json')
    except Exception as e:
        logging.error(f"Error serving source: {str(e)}")
        return jsonify({"error": str(e)}), 404

@app.route('/ipas/<bundle_id>/<filename>')
def serve_ipa(bundle_id, filename):
    """Serve IPA files"""
    try:
        # Security: ensure filename is safe
        safe_bundle_id = secure_filename(bundle_id)
        safe_filename = secure_filename(filename)
        
        filepath = os.path.join(IPA_FOLDER, safe_bundle_id, safe_filename)
        
        if not os.path.exists(filepath):
            return jsonify({"error": "IPA file not found"}), 404
        
        return send_file(filepath, mimetype='application/octet-stream', as_attachment=True, download_name=filename)
    except Exception as e:
        logging.error(f"Error serving IPA: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/qr')
def generate_qr():
    try:
        # Use feather:// URL scheme for QR code
        source_url = request.url_root + 'source.json'
        feather_url = source_url.replace('https://', 'feather://').replace('http://', 'feather://')
        
        qr = qrcode.QRCode(version=1, box_size=10, border=5)
        qr.add_data(feather_url)
        qr.make(fit=True)
        
        img = qr.make_image(fill_color="black", back_color="white")
        buf = io.BytesIO()
        img.save(buf, format='PNG')
        buf.seek(0)
        
        return send_file(buf, mimetype='image/png')
    except Exception as e:
        logging.error(f"QR generation error: {str(e)}")
        return jsonify({"error": "QR generation failed"}), 500

@app.route('/api/apps')
def get_apps():
    try:
        source_data = source_manager.load_source()
        if source_data:
            return jsonify(source_data.get('apps', []))
        return jsonify([])
    except Exception as e:
        logging.error(f"Error getting apps: {str(e)}")
        return jsonify([])

@app.route('/api/add-app', methods=['POST'])
def add_app():
    try:
        base_url = request.url_root.rstrip('/')
        # Check if request has file upload
        if 'ipaFile' in request.files:
            ipa_file = request.files['ipaFile']
            download_from_url = request.form.get('downloadFromUrl', 'false').lower() == 'true'
            data = {
                'name': request.form.get('name'),
                'bundleIdentifier': request.form.get('bundleIdentifier'),
                'developerName': request.form.get('developerName'),
                'localizedDescription': request.form.get('localizedDescription', ''),
                'iconURL': request.form.get('iconURL', ''),
                'version': request.form.get('version'),
                'downloadURL': request.form.get('downloadURL', ''),
                'minOSVersion': request.form.get('minOSVersion', '14.0')
            }
            success, message = source_manager.add_app_manual(data, ipa_file=ipa_file if ipa_file.filename else None, download_from_url=download_from_url, base_url=base_url)
        else:
            # JSON request (backward compatibility)
            data = request.json
            success, message = source_manager.add_app_manual(data, base_url=base_url)
        
        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400
            
    except Exception as e:
        logging.error(f"Error adding app: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/delete-app', methods=['POST'])
def delete_app():
    try:
        data = request.json
        bundle_id = data.get('bundleIdentifier')
        
        if not bundle_id:
            return jsonify({"success": False, "error": "Bundle identifier is required"}), 400
        
        success, message = source_manager.delete_app(bundle_id)
        
        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400
            
    except Exception as e:
        logging.error(f"Error deleting app: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/app/<bundle_identifier>')
def get_app(bundle_identifier):
    try:
        app = source_manager.get_app(bundle_identifier)
        if app:
            return jsonify(app)
        return jsonify({"error": "App not found"}), 404
    except Exception as e:
        logging.error(f"Error getting app: {str(e)}")
        return jsonify({"error": str(e)}), 400

@app.route('/api/update-app', methods=['POST'])
def update_app():
    try:
        # Determine content type and parse data accordingly
        if request.content_type and 'application/json' in request.content_type:
            data = request.json
            ipa_file = None
            download_from_url = False
        else:
            # Handle form data with potential file upload
            data = request.form.to_dict()
            ipa_file = request.files.get('ipaFile')
            download_from_url = request.form.get('downloadFromUrl', 'false').lower() == 'true'
        
        bundle_id = data.get('bundleIdentifier')
        
        if not bundle_id:
            return jsonify({"success": False, "error": "Bundle identifier is required"}), 400
        
        base_url = request.url_root.rstrip('/')
        success, message = source_manager.update_app(bundle_id, data, ipa_file=ipa_file, download_from_url=download_from_url, base_url=base_url)
        
        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400
            
    except Exception as e:
        logging.error(f"Error updating app: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/add-version', methods=['POST'])
def add_version():
    try:
        base_url = request.url_root.rstrip('/')
        # Check if request has file upload
        if 'ipaFile' in request.files:
            ipa_file = request.files['ipaFile']
            download_from_url = request.form.get('downloadFromUrl', 'false').lower() == 'true'
            bundle_id = request.form.get('bundleIdentifier')
            data = {
                'version': request.form.get('version'),
                'downloadURL': request.form.get('downloadURL', ''),
                'minOSVersion': request.form.get('minOSVersion', '')
            }
        else:
            # JSON request (backward compatibility)
            data = request.json
            bundle_id = data.get('bundleIdentifier')
            ipa_file = None
            download_from_url = False
        
        if not bundle_id:
            return jsonify({"success": False, "error": "Bundle identifier is required"}), 400
        
        if not data.get('version'):
            return jsonify({"success": False, "error": "Version is required"}), 400
        
        if not ipa_file and not data.get('downloadURL') and not download_from_url:
            return jsonify({"success": False, "error": "Either IPA file or download URL is required"}), 400
        
        success, message = source_manager.add_version(bundle_id, data, ipa_file=ipa_file if ipa_file and ipa_file.filename else None, download_from_url=download_from_url, base_url=base_url)
        
        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400
            
    except Exception as e:
        logging.error(f"Error adding version: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/update-version', methods=['POST'])
def update_version():
    try:
        data = request.json
        bundle_id = data.get('bundleIdentifier')
        version = data.get('version')
        
        if not bundle_id:
            return jsonify({"success": False, "error": "Bundle identifier is required"}), 400
        
        if not version:
            return jsonify({"success": False, "error": "Version is required"}), 400
        
        if not data.get('downloadURL'):
            return jsonify({"success": False, "error": "Download URL is required"}), 400
        
        success, message = source_manager.update_version(bundle_id, version, data)
        
        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400
            
    except Exception as e:
        logging.error(f"Error updating version: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.route('/api/update-source', methods=['POST'])
def update_source():
    try:
        data = request.json
        success, message = source_manager.update_source_info(data)
        
        if success:
            return jsonify({"success": True, "message": message})
        else:
            return jsonify({"success": False, "error": message}), 400
            
    except Exception as e:
        logging.error(f"Error updating source: {str(e)}")
        return jsonify({"success": False, "error": str(e)}), 400

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error"}), 500

if __name__ == '__main__':
    logging.info("Starting AltStore Source Manager...")
    app.run(host='0.0.0.0', port=5000, debug=False)