export interface RevocationStore {
  isRevoked(subject: string): Promise<boolean>;
}

export class InMemoryRevocationStore implements RevocationStore {
  private readonly subjects = new Set<string>();

  async isRevoked(subject: string): Promise<boolean> {
    return this.subjects.has(subject);
  }

  revoke(subject: string): void {
    this.subjects.add(subject);
  }

  reinstate(subject: string): void {
    this.subjects.delete(subject);
  }
}
