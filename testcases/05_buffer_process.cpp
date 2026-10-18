#include <iostream>
#include <stdexcept>

struct Buffer {
    int* data;
    int size;
};

void fill(Buffer* b, int n) {
    if (n > b->size) {
        throw std::length_error("too many elements");
    }
    for (int i = 0; i < n; ++i) {
        b->data[i] = i * 10;
    }
}

int process(int n) {
    Buffer* b = new Buffer{new int[4], 4};
    fill(b, n);
    int last = b->data[n - 1];
    delete[] b->data;
    delete b;
    return last;
}

int main() {
    std::cout << "process(3) = " << process(3) << "\n";
    try {
        int r = process(10);
        std::cout << "process(10) = " << r << "\n";
    } catch (const std::length_error& e) {
        std::cout << "error: " << e.what() << "\n";
    }
    return 0;
}
