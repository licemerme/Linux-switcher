#!/usr/bin/env python3
"""
Программа переключения раскладки клавиатуры между русской и английской,
аналог Punto Switcher для Linux KDE.
Работает с внешней базой слов для определения нужной раскладки.
"""

import sys
import os
import subprocess
import json
import threading
import time
from PyQt5.QtWidgets import (QApplication, QMainWindow, QSystemTrayIcon, QMenu, 
                          QAction, QVBoxLayout, QWidget, QLabel, QPushButton, QCheckBox, QGridLayout)
from PyQt5.QtGui import QIcon
from PyQt5.QtCore import QTimer, Qt, QSettings
from PyQt5.Qt import QSystemTrayIcon

# Импортируем базу слов
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from word_database import WordDatabase

class LayoutSwitcher(QMainWindow):
    def __init__(self):
        super().__init__()
        self.init_ui()
        self.init_tray()
        self.init_settings()
        
        # Инициализация базы слов
        try:
            self.word_database = WordDatabase()
            # Используем только словарь, если он существует
            if not self.word_database.get_all_words():
                # Создаем пример базы данных
                self.word_database = WordDatabase()
                # Загружаем примерные данные
                sample_words = {
                    'король': 'king', 'корова': 'cow', 'кофе': 'coffee', 'книга': 'book',
                    'дом': 'house', 'свет': 'light', 'вода': 'water', 'песок': 'sand',
                    'солнце': 'sun', 'луна': 'moon', 'звезда': 'star', 'цветок': 'flower',
                    'птица': 'bird', 'рыба': 'fish', 'кошка': 'cat', 'собака': 'dog',
                    'стол': 'table', 'стул': 'chair', 'окно': 'window', 'дверь': 'door',
                    'машина': 'car', 'поезд': 'train', 'самолет': 'airplane', 'лодка': 'boat',
                    'круг': 'circle', 'квадрат': 'square', 'треугольник': 'triangle',
                    'красный': 'red', 'синий': 'blue', 'зеленый': 'green', 'желтый': 'yellow',
                    'черный': 'black', 'белый': 'white', 'серый': 'gray', 'оранжевый': 'orange',
                    'фиолетовый': 'purple', 'розовый': 'pink', 'коричневый': 'brown'
                }
                for russian, english in sample_words.items():
                    self.word_database.add_word(russian, english)
        except Exception as e:
            print(f"Ошибка инициализации базы данных: {e}")
            # Создаем пустую базу в случае ошибки
            self.word_database = WordDatabase()
        
        # Словарь для перевода символов (rus -> eng)
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
        
        # Следим за изменениями раскладки
        self.layout_timer = QTimer()
        self.layout_timer.timeout.connect(self.check_current_layout)
        self.layout_timer.start(1000)  # проверка каждую секунду
        
        # Проверяем текущую раскладку при запуске
        self.check_current_layout()
        
        # Для имитации работы с клавиатурой
        self.last_input = ""
        self.input_buffer = ""
        self.last_word = ""
        
    def init_ui(self):
        self.setWindowTitle('Switcher Advanced')
        self.setFixedSize(400, 300)
        
        layout = QGridLayout()
        
        self.status_label = QLabel('Статус: активен')
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label, 0, 0, 1, 2)
        
        self.layout_label = QLabel('Текущая раскладка: ru')
        self.layout_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.layout_label, 1, 0, 1, 2)
        
        self.word_label = QLabel('Последнее слово: ')
        self.word_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.word_label, 2, 0, 1, 2)
        
        self.toggle_button = QPushButton('Переключить раскладку')
        self.toggle_button.clicked.connect(self.toggle_layout)
        layout.addWidget(self.toggle_button, 3, 0, 1, 2)
        
        self.autostart_checkbox = QCheckBox('Автозапуск')
        self.autostart_checkbox.setChecked(self.autostart)
        self.autostart_checkbox.stateChanged.connect(self.toggle_autostart)
        layout.addWidget(self.autostart_checkbox, 4, 0, 1, 2)
        
        self.test_label = QLabel('Тестовый режим:')
        self.test_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.test_label, 5, 0, 1, 2)
        
        self.test_button = QPushButton('Тестировать')
        self.test_button.clicked.connect(self.test_translation)
        layout.addWidget(self.test_button, 6, 0, 1, 2)
        
        self.debug_label = QLabel('Debug: Нет данных')
        self.debug_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.debug_label, 7, 0, 1, 2)
        
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
    
    def toggle_autostart(self, state):
        # Переключение автозапуска
        self.autostart = bool(state)
        self.settings.setValue('autostart', self.autostart)
        self.auto_start()
        
    def toggle_layout(self):
        # Переключение раскладки
        self.current_layout_index = (self.current_layout_index + 1) % 2
        self.current_layout = 'ru' if self.current_layout_index == 0 else 'us'
        
        # Выполнение команды переключения
        try:
            subprocess.run(self.layout_commands[self.current_layout_index], shell=True, check=True)
        except subprocess.CalledProcessError as e:
            print(f"Ошибка переключения раскладки: {e}")
        except FileNotFoundError:
            print("Команда setxkbmap не найдена")
            
        self.update_layout_label()
        
    def check_current_layout(self):
        # Проверка текущей раскладки
        try:
            result = subprocess.run(['setxkbmap', '-query'], capture_output=True, text=True, check=True)
            if 'layout.*ru' in result.stdout:
                self.current_layout = 'ru'
            elif 'layout.*us' in result.stdout:
                self.current_layout = 'us'
            else:
                self.current_layout = 'unknown'
        except Exception as e:
            print(f"Ошибка проверки раскладки: {e}")
            self.current_layout = 'unknown'
            
        self.update_layout_label()
        
    def update_layout_label(self):
        # Обновление метки раскладки
        self.layout_label.setText(f'Текущая раскладка: {self.current_layout}')
        
    def test_translation(self):
        # Тестирование перевода
        test_text = "Привет мир"
        translated = self.translate_text(test_text)
        print(f"Тест: '{test_text}' -> '{translated}'")
        self.status_label.setText(f'Тест: {test_text} -> {translated}')
        
    def translate_text(self, text):
        # Перевод текста с русского на английский
        if not text:
            return text
            
        # Проверка, если текст содержит русские буквы
        has_russian = any(char in self.translit_dict for char in text)
        
        if has_russian:
            # Перевод с русского на английский
            translated = ''.join(self.translit_dict.get(char, char) for char in text)
            return translated
        else:
            # Для обратного перевода (английский -> русский) можно использовать
            # обратный словарь, но для простоты вернем как есть
            return text
            
    def check_word_in_database(self, word):
        # Проверка, есть ли слово в базе данных
        # Используем только русские слова для проверки
        if self.word_database.has_word(word):
            return True
        # Проверяем, может ли слово быть русским, написанным на английской раскладке
        # Для простоты используем поиск по частичному совпадению
        search_results = self.word_database.search_word(word)
        if search_results:
            return True
        return False
        
    def is_translit_equivalent(self, word, russian_word):
        # Проверяет, может ли слово быть переведённым с помощью транслитерации
        # Это упрощённая версия - в реальном случае нужно использовать более сложные алгоритмы
        translit_word = ''.join(self.translit_dict.get(char, char) for char in russian_word)
        return word.lower() == translit_word.lower()
        
    def correct_and_switch_layout(self, word):
        # Исправление слова и переключение раскладки
        # Проверяем, является ли слово русским, но введённым на английской раскладке
        if self.word_database.has_word(word):
            # Слово уже есть в базе, просто переключаем раскладку
            self.switch_to_russian_layout()
        else:
            # Проверяем, может ли это быть транслитом
            search_results = self.word_database.search_word(word)
            if search_results:
                # Найдено соответствие - переключаем раскладку
                self.switch_to_russian_layout()
                    
    def switch_to_russian_layout(self):
        # Переключение на русскую раскладку
        if self.current_layout != 'ru':
            try:
                subprocess.run(['setxkbmap', '-layout', 'ru'], check=True)
            except subprocess.CalledProcessError as e:
                print(f"Ошибка переключения на русскую раскладку: {e}")
            except FileNotFoundError:
                print("Команда setxkbmap не найдена")
            self.current_layout = 'ru'
            self.update_layout_label()
            
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
    app.setApplicationName('Switcher Advanced')
    app.setApplicationVersion('1.0')
    
    # Создание и запуск программы
    switcher = LayoutSwitcher()
    switcher.show()
    
    sys.exit(app.exec_())

if __name__ == '__main__':
    main()