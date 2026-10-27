#!/bin/bash

# Скрипт установки зависимостей для Switcher

echo "Установка зависимостей для Switcher..."

# Установка PyQt5 через pip
pip install PyQt5

# Проверка установки setxkbmap
if command -v setxkbmap &> /dev/null; then
    echo "setxkbmap установлен"
else
    echo "setxkbmap не найден. Установите x11-xkb-utils"
    sudo apt install x11-xkb-utils
fi

echo "Установка завершена."