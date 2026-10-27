#!/usr/bin/env python3
"""
Скрипт для работы с базой слов для Switcher
"""

import json
import os

class WordDatabase:
    def __init__(self, database_file=None):
        self.database_file = database_file or "/home/seff/Документы/Projects/Switcher_linux/data/words.json"
        self.words = self.load_database()
        
    def load_database(self):
        """Загружает базу слов из файла"""
        # Если файл существует, загружаем его
        if os.path.exists(self.database_file):
            try:
                with open(self.database_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                print(f"Ошибка загрузки базы данных: {e}")
                return {}
        
        # Возвращаем пустую базу, если файл не существует
        return {}
        
    def save_database(self):
        """Сохраняет базу слов в файл"""
        try:
            os.makedirs(os.path.dirname(self.database_file), exist_ok=True)
            with open(self.database_file, 'w', encoding='utf-8') as f:
                json.dump(self.words, f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"Ошибка сохранения базы данных: {e}")
            
    def add_word(self, russian_word, english_translation):
        """Добавляет слово в базу"""
        self.words[russian_word] = english_translation
        self.save_database()
        
    def get_word(self, russian_word):
        """Получает перевод слова"""
        return self.words.get(russian_word)
        
    def has_word(self, russian_word):
        """Проверяет, есть ли слово в базе"""
        return russian_word in self.words
        
    def get_all_words(self):
        """Возвращает все слова"""
        return self.words
        
    def get_common_words(self, count=100):
        """Возвращает наиболее часто используемые слова"""
        # Возвращаем первые N слов из базы
        words_list = list(self.words.items())
        return dict(words_list[:count])
        
    def search_word(self, partial_word):
        """Поиск слова по частичному совпадению"""
        results = {}
        for russian_word, english_word in self.words.items():
            if partial_word.lower() in russian_word.lower():
                results[russian_word] = english_word
        return results

# Создание базы слов (пример)
def create_sample_database():
    """Создает пример базы слов"""
    sample_words = {
        "король": "king",
        "корова": "cow", 
        "кофе": "coffee",
        "книга": "book",
        "дом": "house",
        "свет": "light",
        "вода": "water",
        "песок": "sand",
        "солнце": "sun",
        "луна": "moon",
        "звезда": "star",
        "цветок": "flower",
        "птица": "bird",
        "рыба": "fish",
        "кошка": "cat",
        "собака": "dog",
        "стол": "table",
        "стул": "chair",
        "окно": "window",
        "дверь": "door",
        "машина": "car",
        "поезд": "train",
        "самолет": "airplane",
        "лодка": "boat",
        "круг": "circle",
        "квадрат": "square",
        "треугольник": "triangle",
        "красный": "red",
        "синий": "blue",
        "зеленый": "green",
        "желтый": "yellow",
        "черный": "black",
        "белый": "white",
        "серый": "gray",
        "оранжевый": "orange",
        "фиолетовый": "purple",
        "розовый": "pink",
        "коричневый": "brown"
    }
    
    db = WordDatabase()
    for russian, english in sample_words.items():
        db.add_word(russian, english)
    
    print("Пример базы слов создан")
    return db

if __name__ == "__main__":
    # Создание примера базы данных
    db = create_sample_database()
    print(f"Создано {len(db.get_all_words())} слов")