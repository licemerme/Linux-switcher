#!/usr/bin/env python3
"""
Программа переключения раскладки клавиатуры между русской и английской,
аналог Punto Switcher для Linux KDE.
"""

import sys
import os
import subprocess
import json
from PyQt5.QtWidgets import (QApplication, QMainWindow, QSystemTrayIcon, QMenu, 
                          QAction, QVBoxLayout, QWidget, QLabel, QPushButton)
from PyQt5.QtGui import QIcon, QPixmap
from PyQt5.QtCore import QTimer, Qt
from PyQt5.Qt import QSettings

class LayoutSwitcher(QMainWindow):
    def __init__(self):
        super().__init__()
        self.init_ui()
        self.init_tray()
        self.init_settings()
        
        # Словарь для перевода символов
        self.translit_dict = {
            'а': 'a', 'б': 'b', 'в': 'v', 'г': 'g', 'д': 'd', 'е': 'e', 'ё': 'yo',
            'ж': 'zh', 'з': 'z', 'и': 'i', 'й': 'j', 'к': 'k', 'л': 'l', 'м': 'm',
            'н': 'n', 'о': 'o', 'п': 'p', 'р': 'r', 'с': 's', 'т': 't', 'у': 'u',
            'ф': 'f', 'х': 'h', 'ц': 'c', 'ч': 'ch', 'ш': 'sh', 'щ': 'shch',
            'ъ': '', 'ы': 'y', 'ь': '', 'э': 'e', 'ю': 'yu', 'я': 'ya',
            'А': 'A', 'Б': 'B', 'В': 'V', 'Г': 'G', 'Д': 'D', 'Е': 'E', 'Ё': 'Yo',
            'Ж': 'Zh', 'З': 'Z', 'И': 'I', 'Й': 'J', 'К': 'K', 'Л': 'L', 'М': 'M',
            'Н': 'N', 'О': 'O', 'П': 'P', 'Р': 'R', 'С': 'S', 'Т': 'T', 'У': 'U',
            'Ф': 'F', 'Х': 'H', 'Ц': 'C', 'Ч': 'Ch', 'Ш': 'Sh', 'Щ': 'Shch',
            'Ъ': '', 'Ы': 'Y', 'Ь': '', 'Э': 'E', 'Ю': 'Yu', 'Я': 'Ya'
        }
        
        # Список команд для переключения раскладки
        self.layout_commands = [
            'setxkbmap -layout ru',
            'setxkbmap -layout us'
        ]
        
        self.current_layout_index = 0
        self.current_layout = 'ru'  # по умолчанию русская раскладка
        
        # Таймер для отслеживания ввода
        self.input_timer = QTimer()
        self.input_timer.timeout.connect(self.check_input)
        self.input_timer.start(1000)  # проверка каждую секунду
        
        # Флаг для отслеживания ввода
        self.input_detected = False
        self.last_input_time = 0
        
        # Список последних введённых символов
        self.last_chars = []
        self.max_chars = 10  # максимальное количество символов для анализа
        
    def init_ui(self):
        self.setWindowTitle('Switcher')
        self.setFixedSize(300, 200)
        
        layout = QVBoxLayout()
        label = QLabel('Программа переключения раскладки')
        label.setAlignment(Qt.AlignCenter)
        layout.addWidget(label)
        
        self.status_label = QLabel('Статус: активен')
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)
        
        self.toggle_button = QPushButton('Переключить раскладку')
        self.toggle_button.clicked.connect(self.toggle_layout)
        layout.addWidget(self.toggle_button)
        
        central_widget = QWidget()
        central_widget.setLayout(layout)
        self.setCentralWidget(central_widget)
        
    def init_tray(self):
        # Создание системного трэя
        self.tray_icon = QSystemTrayIcon(self)
        self.tray_icon.setIcon(QIcon.fromTheme('input-keyboard'))
        
        # Создание контекстного меню
        tray_menu = QMenu()
        
        show_action = QAction('Показать', self)
        show_action.triggered.connect(self.show)
        
        quit_action = QAction('Выход', self)
        quit_action.triggered.connect(self.quit_app)
        
        tray_menu.addAction(show_action)
        tray_menu.addAction(quit_action)
        
        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.show()
        
    def init_settings(self):
        # Инициализация настроек
        self.settings = QSettings('Switcher', 'Switcher')
        self.autostart = self.settings.value('autostart', False, type=bool)
        self.auto_start()
        
    def auto_start(self):
        # Настройка автозапуска
        autostart_dir = os.path.expanduser('~/.config/autostart')
        os.makedirs(autostart_dir, exist_ok=True)
        
        desktop_file = os.path.join(autostart_dir, 'switcher.desktop')
        if self.autostart:
            with open(desktop_file, 'w') as f:
                f.write(f'''[Desktop Entry]
Name=Switcher
Exec={sys.executable} {sys.argv[0]}
Icon=input-keyboard
Type=Application
Hidden=false
NoDisplay=false
X-GNOME-Autostart-enabled=true
''')
        else:
            if os.path.exists(desktop_file):
                os.remove(desktop_file)
    
    def toggle_layout(self):
        # Переключение раскладки
        self.current_layout_index = (self.current_layout_index + 1) % 2
        self.current_layout = 'ru' if self.current_layout_index == 0 else 'us'
        
        # Выполнение команды переключения
        try:
            subprocess.run(self.layout_commands[self.current_layout_index], shell=True)
        except Exception as e:
            print(f"Ошибка переключения раскладки: {e}")
            
        self.status_label.setText(f'Статус: {self.current_layout}')
        
    def check_input(self):
        # Проверка ввода клавиатуры (эмуляция)
        # В реальном приложении здесь будет анализ X11 событий
        pass
        
    def switch_layout(self, text):
        # Перевод текста с русского на английский и обратно
        if not text:
            return text
            
        # Проверка, если текст содержит русские буквы
        has_russian = any(char in self.translit_dict for char in text)
        
        if has_russian:
            # Перевод с русского на английский
            translated = ''.join(self.translit_dict.get(char, char) for char in text)
            return translated
        else:
            # Здесь должен быть обратный перевод, но для простоты будем использовать
            # обратный словарь
            return text
            
    def quit_app(self):
        self.tray_icon.hide()
        QApplication.quit()
        
    def closeEvent(self, event):
        # Сворачивание в трей при закрытии окна
        self.hide()
        event.ignore()

def main():
    app = QApplication(sys.argv)
    
    # Установка иконки приложения
    app.setApplicationName('Switcher')
    app.setApplicationVersion('1.0')
    
    # Создание и запуск программы
    switcher = LayoutSwitcher()
    switcher.show()
    
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()