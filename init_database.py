#!/usr/bin/env python3
"""
Скрипт инициализации базы данных для Switcher
"""

import os
import sys
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from word_database import WordDatabase, create_sample_database

def main():
    print("Инициализация базы данных для Switcher...")
    
    # Создаем пример базы данных
    db = create_sample_database()
    
    print("База данных успешно создана")
    print(f"Создано {len(db.get_all_words())} слов")
    
    # Показываем несколько примеров
    words = db.get_common_words(10)
    print("\nПримеры слов:")
    for russian, english in words.items():
        print(f"  {russian} -> {english}")

if __name__ == "__main__":
    main()