#!/bin/bash

# Скрипт запуска Switcher

cd /home/seff/Документы/Projects/Switcher_linux

# Проверка наличия необходимых библиотек
if ! python -c "import PyQt5" &> /dev/null; then
    echo "PyQt5 не установлен. Установите с помощью: pip install PyQt5"
    exit 1
fi

# Проверка наличия словарей
echo "Проверка системных словарей..."
python check_dictionaries.py

# Запуск программы
echo "Запуск Switcher..."
python switcher_system.py