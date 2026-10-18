#include <iostream>

int sum_positive(const int* values, int n) {
    int* copy = new int[n];
    for (int i = 0; i < n; ++i) {
        if (values[i] < 0) {
            return -1;
        }
        copy[i] = values[i];
    }
    int total = 0;
    for (int i = 0; i < n; ++i) {
        total += copy[i];
    }
    delete[] copy;
    return total;
}

int main() {
    int good[] = {1, 2, 3};
    int bad[] = {4, -5, 6};
    std::cout << "good: " << sum_positive(good, 3) << "\n";
    std::cout << "bad: " << sum_positive(bad, 3) << "\n";
    return 0;
}
