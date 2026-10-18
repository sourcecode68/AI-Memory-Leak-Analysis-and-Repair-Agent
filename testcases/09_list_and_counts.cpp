#include <iostream>

struct Node {
    int value;
    Node* next;
};

Node* build_list(int n) {
    Node* head = nullptr;
    for (int i = n; i >= 1; --i) {
        head = new Node{i, head};
    }
    return head;
}

int main() {
    Node* list = build_list(4);
    int sum = 0;
    for (Node* n = list; n != nullptr; n = n->next) {
        sum += n->value;
    }
    std::cout << "sum: " << sum << "\n";

    int* counts = new int[10]();
    counts[3] = 7;
    std::cout << "counts[3]: " << counts[3] << "\n";
    delete[] counts;

    delete list;
    return 0;
}
