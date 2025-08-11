import os
import platform
import requests
from kivy.app import App
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.button import Button
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.clock import mainthread
from kivy.core.window import Window

# Android native filechooser via plyer
try:
    from plyer import filechooser
except ImportError:
    filechooser = None

# Windows native file dialog
if platform.system() == "Windows":
    import tkinter as tk
    from tkinter import filedialog


class StarkLayout(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(orientation='vertical', **kwargs)

        self.status_label = Label(text="Welcome to Stark", size_hint=(1, 0.1))
        self.add_widget(self.status_label)

        self.select_file_btn = Button(text="Select File", size_hint=(1, 0.2))
        self.select_file_btn.bind(on_press=self.open_file_dialog)
        self.add_widget(self.select_file_btn)

        self.selected_file_label = Label(text="No file selected", size_hint=(1, 0.1))
        self.add_widget(self.selected_file_label)

        self.page_input = TextInput(
            hint_text="Pages to print (e.g. 1,3-5 or leave blank for all)",
            size_hint=(1, 0.1),
            multiline=False
        )
        self.add_widget(self.page_input)

        self.upload_btn = Button(text="Upload to Printer", size_hint=(1, 0.1))
        self.upload_btn.bind(on_press=self.upload_file)
        self.add_widget(self.upload_btn)

        self.selected_file = None

    def open_file_dialog(self, instance):
        sys_platform = platform.system()
        if sys_platform == "Windows":
            root = tk.Tk()
            root.withdraw()
            filetypes = [
                ("PDF files", "*.pdf"),
                ("Word Documents", "*.docx"),
                ("PowerPoint Presentations", "*.pptx"),
                ("Excel Spreadsheets", "*.xlsx"),
                ("All files", "*.*"),
            ]
            filepath = filedialog.askopenfilename(title="Select file", filetypes=filetypes)
            root.destroy()
            if filepath:
                self.selected_file = filepath
                self.selected_file_label.text = f"Selected: {os.path.basename(filepath)}"
            else:
                self.selected_file_label.text = "No file selected"

        elif sys_platform == "Linux" or sys_platform == "Darwin":
            # fallback for desktop Linux/Mac - could use Kivy filechooser or plyer if available
            self.selected_file_label.text = "Native dialog not implemented on this platform"
            self.selected_file = None

        elif sys_platform == "Java":  # Android runs on Java VM
            if filechooser:
                filechooser.open_file(on_selection=self.on_file_selected)
            else:
                self.selected_file_label.text = "Plyer filechooser not available"
                self.selected_file = None

        else:
            self.selected_file_label.text = "Unsupported platform"
            self.selected_file = None

    def on_file_selected(self, selection):
        # selection is a list of paths
        if selection and len(selection) > 0:
            self.selected_file = selection[0]
            self.selected_file_label.text = f"Selected: {os.path.basename(self.selected_file)}"
        else:
            self.selected_file_label.text = "No file selected"
            self.selected_file = None

    @mainthread
    def set_status(self, text):
        self.status_label.text = text

    def upload_file(self, instance):
        if not self.selected_file:
            self.set_status("No file selected.")
            return

        pages = self.page_input.text.strip()
        self.set_status("Uploading...")

        try:
            with open(self.selected_file, 'rb') as f:
                files = {'file': (os.path.basename(self.selected_file), f)}
                data = {}
                if pages:
                    data['pages'] = pages
                r = requests.post("http://192.168.1.13:5000/upload", files=files, data=data, timeout=30)

            if r.status_code == 200:
                self.set_status("Upload successful ✅")
            else:
                self.set_status(f"Server error: {r.text}")
        except Exception as e:
            self.set_status(f"Upload failed: {e}")


class StarkApp(App):
    def build(self):
        self.title = "Stark Print Uploader"
        Window.size = (500, 300)
        return StarkLayout()


if __name__ == "__main__":
    StarkApp().run()
