#!/usr/bin/env python3
"""
Проверка наличия системных словарей
"""

import os

def check_system_dictionaries():
    """Проверяет наличие системных словарей"""
    print("Проверка системных словарей...")
    
    # Проверяем основные словари
    dict_paths = [
        "/usr/share/dict/words",
        "/usr/share/dict/linux.words"
    ]
    
    found_dicts = []
    for path in dict_paths:
        if os.path.exists(path):
            print(f"✓ Найден словарь: {path}")
            found_dicts.append(path)
        else:
            print(f"✗ Словарь не найден: {path}")
    
    if found_dicts:
        print(f"\nНайдено {len(found_dicts)} словарей")
        return True
    else:
        print("\nСистемные словари не найдены")
        return False

if __name__ == "__main__":
    check_system_dictionaries()