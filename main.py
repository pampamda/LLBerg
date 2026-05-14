import sys
from PyQt6.QtWidgets import QApplication
from pet_window import PetWindow


def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    pet = PetWindow()
    pet.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
