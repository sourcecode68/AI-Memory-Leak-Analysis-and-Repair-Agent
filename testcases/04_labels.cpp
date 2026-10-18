#include <iostream>
#include <string>

std::string* make_label(int i) {
    return new std::string("item-" + std::to_string(i));
}

int main() {
    for (int i = 0; i < 5; ++i) {
        std::string* label = make_label(i);
        std::cout << *label << "\n";
    }
    return 0;
}
